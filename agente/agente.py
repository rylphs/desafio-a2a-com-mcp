"""Agente Central de Salas: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro.

    uv run python agente/agente.py

Variáveis de ambiente (todas opcionais):
    AGENTE_HOST       interface de escuta             (padrão 127.0.0.1)
    AGENTE_PORT       porta do agente                 (padrão 7300)
    AGENTE_URL        URL pública anunciada no card   (padrão http://localhost:7300)
    MCP_URL           endpoint do servidor MCP        (padrão http://localhost:7301/mcp)
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import SendMessageRequest, Task
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_1_0, VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request

from cliente_mcp import ClienteMCP
from ponte import AgenteDeSalas

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://localhost:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
CAMINHO_A2A = "/a2a"

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("agente")

CARD = AgentCard(
    name="Central de Salas",
    description="Reserva salas de reuniao da Hill Valley Tech.",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    version="1.0.0",
    supported_interfaces=[AgentInterface(url=URL_PUBLICA + CAMINHO_A2A, protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="reservar-sala",
            name="Reservar sala",
            description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            tags=["salas", "agenda"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            examples=[
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        )
    ],
)


class ContextoA2A10(DefaultServerCallContextBuilder):
    """Trata a ausência do header `A2A-Version` como 1.0.

    O SDK segue a regra da spec e lê header ausente como 0.3, recusando a chamada
    no handler 1.0. Este endpoint só fala 1.0, que é a versão declarada no card
    para ele, e o cliente do validador não envia o header. Um `A2A-Version`
    explícito continua valendo e continua sendo validado pelo SDK.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        headers = contexto.state["headers"]
        if not (headers.get(VERSION_HEADER) or headers.get(VERSION_HEADER.lower())):
            headers[VERSION_HEADER.lower()] = PROTOCOL_VERSION_1_0
        return contexto


class ContextoDaTask(SimpleRequestContextBuilder):
    """Continuação que traz só o `taskId` herda o `contextId` da Task.

    A spec manda inferir o contexto a partir da Task referenciada; o SDK gera um
    `contextId` novo para a mensagem, que então diverge do da Task.
    """

    def __init__(self, task_store: TaskStore) -> None:
        super().__init__(task_store=task_store)
        self._tasks = task_store

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        if params is not None and params.message.task_id and not params.message.context_id:
            existente = await self._tasks.get(params.message.task_id, context)
            if existente is not None:
                params.message.context_id = context_id = existente.context_id
        return await super().build(context, params, task_id, context_id, task)


def criar_app() -> Starlette:
    cliente = ClienteMCP(MCP_URL)
    tasks = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=AgenteDeSalas(cliente),
        task_store=tasks,
        agent_card=CARD,
        request_context_builder=ContextoDaTask(tasks),
    )

    @asynccontextmanager
    async def ciclo_de_vida(_: Starlette) -> AsyncIterator[None]:
        await cliente.abrir()
        log.info("agente A2A em %s%s, falando MCP com %s", URL_PUBLICA, CAMINHO_A2A, MCP_URL)
        try:
            yield
        finally:
            await cliente.fechar()

    rotas = create_agent_card_routes(CARD) + create_jsonrpc_routes(handler, CAMINHO_A2A, context_builder=ContextoA2A10())
    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


if __name__ == "__main__":
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")
