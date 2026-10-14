"""O agente como host MCP: um cliente do SDK oficial falando Streamable HTTP com o servidor de salas.

Duas escolhas importam aqui:

- O cliente não registra callback de elicitation e nunca usa `Client.call_tool`,
  que resolveria o `input_required` sozinho. As chamadas usam
  `ClientSession.call_tool(..., allow_input_required=True)`, que devolve o
  `InputRequiredResult` cru para a ponte decidir o que fazer.
- A capability declarada em todo request é exatamente `{"elicitation": {"form": {}}}`.
  O SDK só anuncia elicitation quando há callback, e nesse caso anuncia form e
  url juntos. A sessão abaixo sobrescreve esse único ponto.

O `_meta` obrigatório (`protocolVersion`, `clientCapabilities`, `clientInfo`) e os
headers espelhados (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`) são
carimbados pelo SDK em cada request. Daqui só sai o `traceparent`, por chamada.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any, TypeVar

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

PROTOCOLO = "2026-07-28"
CLIENT_INFO = types.Implementation(name="agente-central-de-salas", version="1.0.0")

log = logging.getLogger("agente.mcp")

R = TypeVar("R")


class _SessaoComElicitationForm(ClientSession):
    """Declara elicitation em form mode, e só form, sem registrar callback que a responda."""

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        capacidades = super()._build_capabilities(version)
        return capacidades.model_copy(
            update={"elicitation": types.ElicitationCapability(form=types.FormElicitationCapability())}
        )


class ClienteMCP:
    """Um objeto cliente vivo durante todo o processo. Nenhum estado de protocolo fica nele:
    versão e capabilities viajam em cada request, e o servidor é stateless. Se a conexão
    cai (servidor MCP reiniciado no meio de uma chamada), a próxima chamada reabre."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._pilha: AsyncExitStack | None = None
        self._sessao: ClientSession | None = None
        self._tools: dict[str, types.Tool] | None = None
        self._trava = anyio.Lock()

    async def abrir(self) -> None:
        async with self._trava:
            if self._sessao is None:
                await self._conectar()

    async def fechar(self) -> None:
        async with self._trava:
            await self._descartar()

    async def _conectar(self) -> None:
        pilha = AsyncExitStack()
        leitura, escrita = await pilha.enter_async_context(streamable_http_client(self.url, terminate_on_close=False))
        sessao = _SessaoComElicitationForm(dispatcher=JSONRPCDispatcher(leitura, escrita), client_info=CLIENT_INFO)
        await pilha.enter_async_context(sessao)
        # Versão fixada na revisão exigida, sem handshake: o servidor é stateless e
        # a versão vai no `_meta` de cada request.
        sessao.adopt(
            types.DiscoverResult(
                supported_versions=[PROTOCOLO],
                capabilities=types.ServerCapabilities(),
                result_type="complete",
                ttl_ms=0,
                cache_scope="private",
            )
        )
        self._pilha, self._sessao = pilha, sessao

    async def _descartar(self) -> None:
        pilha, self._pilha, self._sessao = self._pilha, None, None
        if pilha is not None:
            try:
                await pilha.aclose()
            except Exception:  # transporte que já caiu falha ao fechar; não há o que salvar
                log.debug("erro ao fechar a conexao MCP", exc_info=True)

    async def _usar(self, operacao: Callable[[ClientSession], Awaitable[R]]) -> R:
        async with self._trava:
            if self._sessao is None:
                await self._conectar()
            sessao = self._sessao
        assert sessao is not None
        try:
            return await operacao(sessao)
        except MCPError as erro:
            if erro.code == types.CONNECTION_CLOSED:
                # Não repete a chamada: um tools/call pode ter chegado ao servidor.
                async with self._trava:
                    if self._sessao is sessao:
                        await self._descartar()
            raise

    async def descobrir_tools(self, meta: dict[str, Any]) -> dict[str, types.Tool]:
        """`tools/list` em runtime, feito antes da primeira chamada de tool."""
        if self._tools is None:
            resultado = await self._usar(lambda s: s.list_tools(params=types.PaginatedRequestParams(_meta=meta)))
            self._tools = {t.name: t for t in resultado.tools}
            log.info("tools descobertas: %s", ", ".join(sorted(self._tools)))
        return self._tools

    async def ler_resource(self, uri: str, meta: dict[str, Any]) -> str:
        resultado = await self._usar(lambda s: s.read_resource(uri, meta=meta))
        assert isinstance(resultado, types.ReadResourceResult)
        return "".join(c.text for c in resultado.contents if isinstance(c, types.TextResourceContents))

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        meta: dict[str, Any],
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """Um `tools/call`. Cada chamada é um request JSON-RPC novo, com id novo, inclusive o retry."""
        resultado = await self._usar(
            lambda s: s.call_tool(
                nome,
                argumentos,
                input_responses=input_responses,
                request_state=request_state,
                meta=meta,
                allow_input_required=True,
            )
        )
        assert isinstance(resultado, types.CallToolResult | types.InputRequiredResult)
        return resultado
