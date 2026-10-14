"""Servidor MCP da Central de Salas (Streamable HTTP, revisão 2026-07-28).

Três tools, um resource e o ciclo de MRTR na reserva. O MRTR não é feito à mão:
`reservar_sala` declara um parâmetro preenchido pelo resolver `escolha_de_sala`.
Quando o resolver devolve `Elicit(...)`, o SDK encerra a resposta com
`resultType: input_required`, um `inputRequests` em form mode e um
`requestState`. O `RequestStateBoundary` do SDK sela esse estado com AES-256-GCM
sob a chave de `REQUEST_STATE_SECRET` e o verifica no retry. Nada fica em
memória entre a pergunta e o retry.

    REQUEST_STATE_SECRET=... uv run python servidor-mcp/servidor.py
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

import dominio
from dominio import RegraVioladaError

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

NOME = "central-de-salas"
VERSAO = "1.0.0"
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORT", "7301"))
CAMINHO = os.environ.get("MCP_PATH", "/mcp")
# Validade do requestState: 10 minutos, dentro da faixa de 5 a 30 que o enunciado pede.
TTL_REQUEST_STATE = 600.0

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger(NOME)


def _chave_de_integridade() -> bytes:
    """Lê a chave de `REQUEST_STATE_SECRET`. Sem chave, o servidor não sobe."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit('REQUEST_STATE_SECRET nao definido. Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"')
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < 32:
        sys.exit(f"REQUEST_STATE_SECRET precisa de pelo menos 32 bytes de aleatoriedade, veio {len(chave)}")
    return chave


def registrar_requests(app: Any) -> Any:
    """Middleware ASGI: registra no stderr método, id e traceparent de cada request.

    Fica na borda HTTP, e não no middleware MCP, para que também apareçam os
    requests que o SDK recusa antes do handler: `_meta` incompleto, header que
    não bate com o corpo, `requestState` adulterado.
    """

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await app(scope, receive, send)
            return
        mensagens, corpo, mais = [], b"", True
        while mais:
            mensagem = await receive()
            mensagens.append(mensagem)
            corpo += mensagem.get("body", b"")
            mais = mensagem.get("more_body", False)
        try:
            dados = json.loads(corpo)
        except ValueError:
            dados = None
        for item in dados if isinstance(dados, list) else [dados]:
            if isinstance(item, dict) and "method" in item:
                meta = (item.get("params") or {}).get("_meta") or {}
                traceparent = meta.get("traceparent", "-") if isinstance(meta, dict) else "-"
                log.info("request method=%s id=%s traceparent=%s", item["method"], item.get("id"), traceparent)

        async def reenviar() -> dict:
            return mensagens.pop(0) if mensagens else await receive()

        await app(scope, reenviar, send)

    return asgi


mcp = MCPServer(
    NOME,
    version=VERSAO,
    request_state_security=RequestStateSecurity(keys=[_chave_de_integridade()], ttl=TTL_REQUEST_STATE),
)


# ---------------------------------------------------------------------------
# Esquemas de saída
# ---------------------------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _validar(sala: str, inicio: str, fim: str) -> dominio.Pedido:
    try:
        return dominio.validar_reserva(sala, inicio, fim)
    except RegraVioladaError as erro:
        raise ToolError(str(erro)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**dataclasses.asdict(s)) for s in dominio.salas])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    pedido = _validar(sala, inicio, fim)
    em_conflito = dominio.listar_conflitos(pedido)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in em_conflito],
    )


def _modelo_de_escolha(opcoes: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: uma propriedade `sala` restrita às alternativas, na ordem."""
    return create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[BaseModel] | None:
    """Resolver do MRTR. Roda a cada rodada, antes do corpo da tool.

    Intervalo livre: devolve None e a tool reserva a sala pedida. Intervalo
    ocupado: devolve `Elicit`, e o SDK transforma isso em `input_required`. No
    retry, o SDK casa a resposta com a pergunta pela chave e por um digest da
    pergunta selado no `requestState`, e injeta o resultado na tool.
    """
    pedido = _validar(sala, inicio, fim)
    if not dominio.listar_conflitos(pedido):
        return None
    opcoes = dominio.obter_alternativas(pedido)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", _modelo_de_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        # decline ou cancel: conclui normalmente, sem reservar e sem isError.
        return ReservaOut(reservado=False, motivo="recusado")
    destino = sala if escolha.data is None else escolha.data.sala
    pedido = _validar(destino, inicio, fim)
    if dominio.listar_conflitos(pedido):
        # A alternativa foi ocupada entre a pergunta e o retry.
        raise ToolError(f"Sala ocupada no intervalo: {destino}")
    reserva = dominio.criar_reserva(destino, inicio, fim, responsavel)
    log.info("reserva criada %s", json.dumps(reserva, ensure_ascii=False))
    return ReservaOut(
        reserva=reserva["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=dominio.POLITICA_VERSAO,
    )


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


if __name__ == "__main__":
    import uvicorn

    # Stateless e com resposta JSON: cada POST é um request completo, sem sessão e sem SSE.
    app = mcp.streamable_http_app(streamable_http_path=CAMINHO, json_response=True, stateless_http=True, host=HOST)
    log.info("servidor MCP em http://%s:%d%s", HOST, PORTA, CAMINHO)
    uvicorn.run(registrar_requests(app), host=HOST, port=PORTA, log_level="warning")
