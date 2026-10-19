"""A ponte: a Task do A2A por fora, o MRTR do MCP por dentro.

O agente não tem regra de sala. Ele interpreta um pedido em formato fixo, chama
o servidor MCP e traduz o resultado em estado de Task:

    CallToolResult ok               -> TASK_STATE_COMPLETED, artifact "reserva"
    CallToolResult isError          -> TASK_STATE_FAILED, com o texto da tool
    CallToolResult reservado=false  -> TASK_STATE_CANCELED
    InputRequiredResult             -> TASK_STATE_INPUT_REQUIRED, "alternativas: ..."

No `input_required`, o `requestState` e a chave do `inputRequests` ficam
guardados em `self._pausas`, indexados pelo id da Task, e não saem do agente.
Na continuação (`escolha=...`), o mesmo `tools/call` é repetido com id novo,
levando `inputResponses` com a mesma chave e o `requestState` como veio.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import mcp.types as mcp_types
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from mcp.shared.exceptions import MCPError

from cliente_mcp import ClienteMCP

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-([0-9a-f]{2})$")
RECUSAR = "recusar"

log = logging.getLogger("agente.executor_reservas")


@dataclass
class Pausa:
    """O que a Task interrompida guarda para retomar o MRTR. O `request_state` é opaco:
    guardado e ecoado sem ser aberto, interpretado ou reconstruído."""

    argumentos: dict[str, str]
    chave: str
    request_state: str
    alternativas: list[str]
    politica: str


@dataclass
class Trace:
    trace_id: str
    flags: str

    def traceparent(self) -> str:
        """Mesmo trace-id do cliente A2A, span-id novo a cada request MCP."""
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class AgenteDeSalas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        self._pausas: dict[str, Pausa] = {}
        self._traces: dict[str, Trace] = {}

    # -- A2A ------------------------------------------------------------------

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id and context.message
        tarefa = context.current_task
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        self._registrar_trace(context)
        texto = context.get_user_input().strip()

        if tarefa is None:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message],
                )
            )
            await updater.start_work()
            await self._novo_pedido(context.task_id, texto, updater)
        else:
            await self._continuacao(context.task_id, texto, updater)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        self._encerrar(context.task_id)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    # -- Fluxo ----------------------------------------------------------------

    async def _novo_pedido(self, task_id: str, texto: str, updater: TaskUpdater) -> None:
        casou = PEDIDO.match(texto)
        if not casou:
            await self._terminar(
                task_id,
                updater,
                TaskState.TASK_STATE_REJECTED,
                "Pedido invalido. Formato: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
            )
            return
        sala, inicio, fim, responsavel = casou.groups()
        argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}
        try:
            tools = await self.mcp.descobrir_tools(self._meta(task_id))
            if TOOL_RESERVA not in tools:
                await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, f"Servidor MCP sem a tool {TOOL_RESERVA}")
                return
            politica = _versao_da_politica(await self.mcp.ler_resource(URI_POLITICA, self._meta(task_id)))
            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, self._meta(task_id))
        except Exception as erro:
            await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, _motivo_da_falha(erro))
            return
        await self._traduzir(task_id, argumentos, politica, resultado, updater)

    async def _continuacao(self, task_id: str, texto: str, updater: TaskUpdater) -> None:
        pausa = self._pausas.get(task_id)
        if pausa is None:
            await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, "Task sem pergunta pendente para retomar")
            return
        casou = ESCOLHA.match(texto)
        escolha = casou.group(1) if casou else None
        if escolha != RECUSAR and escolha not in pausa.alternativas:
            # Fora do enum: a Task continua interrompida e a pergunta se repete.
            await updater.requires_input(_mensagem(updater, _linha_de_alternativas(pausa.alternativas)))
            return

        await updater.start_work()
        if escolha == RECUSAR:
            resposta = mcp_types.ElicitResult(action="decline")
        else:
            resposta = mcp_types.ElicitResult(action="accept", content={"sala": escolha})
        try:
            resultado = await self.mcp.chamar_tool(
                TOOL_RESERVA,
                pausa.argumentos,
                self._meta(task_id),
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except Exception as erro:
            await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, _motivo_da_falha(erro))
            return
        await self._traduzir(task_id, pausa.argumentos, pausa.politica, resultado, updater)

    async def _traduzir(
        self,
        task_id: str,
        argumentos: dict[str, str],
        politica: str,
        resultado: mcp_types.CallToolResult | mcp_types.InputRequiredResult,
        updater: TaskUpdater,
    ) -> None:
        """Onde o resultado do MCP vira estado de Task."""
        if isinstance(resultado, mcp_types.InputRequiredResult):
            pausa = _pausa_de(argumentos, politica, resultado)
            if pausa is None:
                await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, "Pedido de input do servidor MCP nao suportado")
                return
            self._pausas[task_id] = pausa
            log.info("task %s interrompida: alternativas %s", task_id, pausa.alternativas)
            await updater.requires_input(_mensagem(updater, _linha_de_alternativas(pausa.alternativas)))
            return

        texto = " ".join(c.text for c in resultado.content if isinstance(c, mcp_types.TextContent))
        if resultado.is_error:
            await self._terminar(task_id, updater, TaskState.TASK_STATE_FAILED, texto)
            return
        dados: dict[str, Any] = resultado.structured_content or json.loads(texto)
        if not dados.get("reservado"):
            await self._terminar(task_id, updater, TaskState.TASK_STATE_CANCELED, f"Reserva nao realizada: {dados.get('motivo')}")
            return

        reserva = {
            "reserva": dados["reserva"],
            "sala": dados["sala"],
            "inicio": dados["inicio"],
            "fim": dados["fim"],
            "responsavel": dados["responsavel"],
            "politica": politica,
        }
        await updater.add_artifact([Part(text=json.dumps(reserva, ensure_ascii=False))], name="reserva")
        await self._terminar(
            task_id, updater, TaskState.TASK_STATE_COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."
        )

    async def _terminar(self, task_id: str, updater: TaskUpdater, estado: int, texto: str) -> None:
        """Leva a Task a um estado terminal com a mensagem final também no histórico.

        O SDK só move a mensagem de status para o `history` quando chega o status
        seguinte, e depois de um estado terminal não chega outro. A mensagem sai
        primeiro num status WORKING e depois no terminal: assim ela fica no
        `history` uma vez só (como o erro da tool, que precisa estar visível ali)
        e também em `status.message`.
        """
        self._encerrar(task_id)
        log.info("task %s -> %s: %s", task_id, TaskState.Name(estado), texto)
        mensagem = _mensagem(updater, texto)
        await updater.update_status(TaskState.TASK_STATE_WORKING, message=mensagem)
        await updater.update_status(estado, message=mensagem)

    def _encerrar(self, task_id: str) -> None:
        self._pausas.pop(task_id, None)
        self._traces.pop(task_id, None)

    # -- Trace context --------------------------------------------------------

    def _registrar_trace(self, context: RequestContext) -> None:
        """Usa o trace-id do header `traceparent` da chamada A2A. Sem header, a Task
        mantém o trace que já tinha, ou ganha um novo na primeira mensagem."""
        assert context.task_id
        headers = context.call_context.state.get("headers") or {}
        casou = TRACEPARENT.match(str(headers.get("traceparent", "")).strip().lower())
        if casou and casou.group(1) != "0" * 32:
            self._traces[context.task_id] = Trace(casou.group(1), casou.group(2))
        else:
            self._traces.setdefault(context.task_id, Trace(secrets.token_hex(16), "01"))

    def _meta(self, task_id: str) -> dict[str, Any]:
        return {"traceparent": self._traces[task_id].traceparent()}


def _motivo_da_falha(erro: Exception) -> str:
    """Erro de protocolo do MCP vai com a mensagem do servidor; queda de conexão, com aviso claro."""
    if isinstance(erro, MCPError) and erro.code != mcp_types.CONNECTION_CLOSED:
        return erro.message
    log.warning("falha ao falar com o servidor MCP: %r", erro)
    return f"Servidor MCP indisponivel: {erro}"


def _versao_da_politica(texto: str) -> str:
    """A primeira linha do resource declara a versão: `versao: 2026-11-01`."""
    primeira = texto.splitlines()[0] if texto else ""
    chave, _, valor = primeira.partition(":")
    if chave.strip() != "versao" or not valor.strip():
        raise MCPError(code=-32603, message="Resource politica://uso sem versao na primeira linha")
    return valor.strip()


def _pausa_de(argumentos: dict[str, str], politica: str, resultado: mcp_types.InputRequiredResult) -> Pausa | None:
    """Lê a única elicitation em form mode e as alternativas do `enum` (ou `const`), na ordem."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or not resultado.request_state:
        return None
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, mcp_types.ElicitRequest) or not isinstance(pedido.params, mcp_types.ElicitRequestFormParams):
        return None
    campo = pedido.params.requested_schema.get("properties", {}).get("sala", {})
    alternativas = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    if not alternativas:
        return None
    return Pausa(argumentos, chave, resultado.request_state, [str(a) for a in alternativas], politica)


def _linha_de_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def _mensagem(updater: TaskUpdater, texto: str) -> Any:
    return updater.new_agent_message([Part(text=texto)])
