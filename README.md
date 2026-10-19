# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio da Hill Valley Tech: cinco salas de reunião expostas como capacidade de agente.

- **`servidor-mcp/`**: servidor MCP (revisão `2026-07-28`) em Streamable HTTP na porta `7301`, com três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), o resource `politica://uso` e o ciclo de MRTR na reserva.
- **`agente/`**: agente que é host MCP por dentro e servidor A2A v1.0 (JSON-RPC) por fora, na porta `7300`. Ele publica o Agent Card, atende `SendMessage` e `GetTask` e transforma o `input_required` do MCP em `TASK_STATE_INPUT_REQUIRED`.

São dois processos separados. O agente fala com o servidor só por HTTP, como qualquer cliente MCP. Não há LLM no caminho: o agente interpreta um pedido em formato fixo e decide por regra.

Stack, com versões travadas em `pyproject.toml` e `uv.lock`:

| Peça | Pacote |
|---|---|
| Servidor e cliente MCP | `mcp==2.3.0` (SDK oficial v2, revisão `2026-07-28`) |
| Servidor A2A | `a2a-sdk[http-server]==1.2.2` (SDK oficial, A2A v1.0) |
| HTTP | `uvicorn==0.37.0` (Starlette vem pelos SDKs) |
| Python | 3.14, fixado em `.python-version` por ser a versão validada (o projeto aceita 3.10 ou superior; também validado em 3.10, 3.11 e 3.12) |

## Como rodar

Pré-requisito: [uv](https://docs.astral.sh/uv/getting-started/installation/). Ele instala o Python fixado em `.python-version`, se faltar, e as dependências travadas no `uv.lock`.

```bash
git clone https://github.com/rylphs/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync --frozen
```

**Terminal 1: servidor MCP.** A chave de integridade do `requestState` vem de `REQUEST_STATE_SECRET` e precisa ter pelo menos 32 bytes aleatórios. Gere a sua, nunca use uma que esteja em repositório:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
uv run python servidor-mcp/servidor.py
```

O stderr desse processo registra cada request recebido, com método, id e `traceparent`.

**Terminal 2: agente.**

```bash
uv run python agente/agente.py
```

**Terminal 3: validador.** Rode com os dois processos recém-iniciados, porque as reservas criadas numa execução mudam o resultado da seguinte.

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Para conferir que o `requestState` continua válido depois de um restart, reinicie o servidor MCP com o **mesmo** `REQUEST_STATE_SECRET` entre o `input_required` e o retry.

Variáveis opcionais, todas com os padrões do enunciado:

| Variável | Processo | Padrão |
|---|---|---|
| `MCP_HOST`, `MCP_PORT`, `MCP_PATH` | servidor MCP | `127.0.0.1`, `7301`, `/mcp` |
| `AGENTE_HOST`, `AGENTE_PORT` | agente | `127.0.0.1`, `7300` |
| `AGENTE_URL` | agente (URL anunciada no card) | `http://localhost:7300` |
| `MCP_URL` | agente | `http://localhost:7301/mcp` |

## Onde a ponte acontece

A ponte está em `agente/ponte.py`, no método `AgenteDeSalas._traduzir` (`agente/ponte.py:158`). O cliente MCP do agente chama `ClientSession.call_tool(..., allow_input_required=True)` (`agente/cliente_mcp.py:143`) e não registra callback de elicitation. Por isso o `input_required` chega cru, em vez de ser respondido pelo próprio SDK.

Quando o resultado é um `InputRequiredResult` (`agente/ponte.py:167`), `_pausa_de` (`agente/ponte.py:252`) lê a única elicitation em form mode e as alternativas do `enum`, na ordem recebida. O agente guarda a chave do `inputRequests`, o `requestState` (opaco, nunca aberto), os argumentos originais e a versão da política em `self._pausas[task_id]` (`agente/ponte.py:172`). Em seguida chama `updater.requires_input(...)` com a linha `alternativas: ...` (`agente/ponte.py:174`). Esse é o ponto em que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`.

O caminho de volta está em `AgenteDeSalas._continuacao` (`agente/ponte.py:128`). O `SendMessage` com `taskId` e `escolha=<id>` recupera a pausa daquela Task. `escolha=recusar` vira `action: decline`, e uma escolha fora do `enum` mantém a Task pausada e repete a lista (`agente/ponte.py:137`). Uma escolha válida repete o mesmo `tools/call` com `inputResponses` na mesma chave e o `requestState` ecoado sem modificação (`agente/ponte.py:150-151`). O id JSON-RPC é novo, porque cada chamada do SDK é um request independente. O resultado volta para `_traduzir` e a Task termina em `COMPLETED`, `CANCELED` ou `FAILED`.

Do lado do servidor, o MRTR vem do próprio SDK. `reservar_sala` declara um parâmetro `Annotated[ElicitationResult[...], Resolve(escolha_de_sala)]` (`servidor-mcp/servidor.py:199`). O resolver `escolha_de_sala` (`servidor-mcp/servidor.py:181`) valida o pedido e, se houver conflito, devolve `Elicit(...)` com um schema plano cujo `enum` são as alternativas. A partir disso o SDK monta o `input_required`, o `inputRequests` e o `requestState`, e no retry injeta a resposta na tool.

## Decisões técnicas

**Proteção do `requestState`.** O servidor usa o `RequestStateBoundary` do SDK, configurado com `RequestStateSecurity(keys=[chave], ttl=600)` (`servidor-mcp/servidor.py:101`). Cada `requestState` emitido é cifrado e autenticado com AES-256-GCM, com chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET`. O servidor não sobe se a variável faltar ou tiver menos de 32 bytes (`servidor-mcp/servidor.py:48`). Dentro do envelope selado vão:
- a expiração (`iat` e `exp`);
- o método e a tool;
- um digest dos argumentos originais;
- a audiência (o nome do servidor);
- as respostas já dadas e um digest da pergunta feita.

No retry, qualquer alteração faz a verificação falhar com `-32602 Invalid or expired requestState`, e o motivo real vai só para o log. Isso vale para um caractere trocado, um token vencido, outra tool ou argumentos diferentes dos selados. Argumentos adulterados no retry são rejeitados, e o servidor nunca reserva com eles. Como tudo viaja no token, o servidor não guarda nada entre a pergunta e o retry, e um retry depois de reiniciar o processo funciona, desde que o segredo seja o mesmo.

**Validade.** São 10 minutos (`TTL_REQUEST_STATE = 600`, `servidor-mcp/servidor.py:42`), dentro da faixa de 5 a 30 minutos.

**Estado das Tasks.** As Tasks (id, `contextId`, status, histórico e artifacts) ficam no `InMemoryTaskStore` do `a2a-sdk`. O estado da ponte fica em dois dicionários do `AgenteDeSalas`, indexados pelo id da Task:
- `_pausas`: chave do `inputRequests`, `requestState`, argumentos, alternativas e versão da política;
- `_traces`: trace-id da Task.

Por isso duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. O `requestState` não sai do agente: não vai para card, mensagem nem artifact. Tudo é removido quando a Task chega a um estado terminal. As reservas do servidor MCP ficam em memória (`servidor-mcp/dominio.py`) e não sobrevivem a restart, como o enunciado permite.

**Capability de elicitation.** O cliente MCP do SDK só anuncia elicitation quando há callback, e nesse caso anuncia `form` e `url` juntos. Mas o agente não pode ter callback, porque ele fecharia o ciclo sozinho. A subclasse `_SessaoComElicitationForm` (`agente/cliente_mcp.py:40`) sobrescreve só a montagem das capabilities. Assim todo request sai com exatamente `{"elicitation": {"form": {}}}`. O resto do `_meta` e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` são carimbados pelo SDK em cada request. A versão `2026-07-28` é fixada sem handshake, porque o servidor é stateless.

**Descoberta e contexto.** Antes da primeira chamada de tool, o agente faz `tools/list` e confere que `reservar_sala` existe. A cada pedido novo, lê `politica://uso` e extrai a versão da primeira linha. Essa versão vai para o campo `politica` do artifact `reserva`. As regras de sala (conflito, política, alternativas) ficam todas no servidor MCP. O agente só traduz protocolo.

**`traceparent`.** O agente lê o header `traceparent` da chamada A2A e guarda o trace-id da Task (`agente/ponte.py:220`). Todo request MCP daquela Task leva `traceparent` no `_meta`, com o mesmo trace-id e um span-id novo. Uma continuação sem o header continua usando o trace da Task. O servidor registra método, id e `traceparent` no stderr por um middleware ASGI (`servidor-mcp/servidor.py:62`). O log fica na borda HTTP para registrar também os requests que o SDK recusa antes do handler, como `_meta` incompleto, header divergente ou `requestState` adulterado.

**Erros.** Erros de execução da tool (`isError: true`) terminam a Task em `TASK_STATE_FAILED`, e o texto da tool fica em `status.message` e no `history`. O SDK do MCP prefixa a mensagem com `Error executing tool <nome>:`, o que o enunciado aceita. Uma tool inexistente volta como `isError`. URI inexistente, `_meta` incompleto e `requestState` inválido voltam como erro JSON-RPC `-32602`, e capability faltando volta como `-32021`. Todos esses erros vêm do próprio SDK, com HTTP 400. Se o servidor MCP cai no meio de uma chamada, a Task termina em `FAILED` e a próxima chamada reabre a conexão. A chamada que falhou não é repetida, porque um `tools/call` pode ter chegado ao servidor.

### Ajustes de SDK documentados

Pontos em que os SDKs divergem do que o desafio exige. Os três primeiros foram resolvidos pelos pontos de extensão públicos dos SDKs, sem reescrever o protocolo. O quarto é só registro:

1. **`A2A-Version` ausente.** O `a2a-sdk` lê header ausente como `0.3` e recusa a chamada no handler 1.0, com a mensagem `A2A version '0.3' is not supported by this handler. Expected version '1.0'.`. O validador não envia o header. Como `/a2a` só fala 1.0, que é o que o card declara, `ContextoA2A10` (`agente/agente.py:69`) trata a ausência do header como `1.0`. Um header explícito continua sendo validado pelo SDK.
2. **`contextId` da continuação.** Quando a mensagem traz só `taskId`, o `a2a-sdk` gera um `contextId` novo em vez de herdar o da Task. Com isso a mensagem fica com contexto diferente e o `TaskUpdater` falha com `Context in event doesn't match TaskManager`. `ContextoDaTask` (`agente/agente.py:86`) preenche o `contextId` a partir da Task armazenada.
3. **Mensagem final no histórico.** O `a2a-sdk` só move a mensagem de status para o `history` quando chega o status seguinte, e depois de um estado terminal não chega outro. `_terminar` (`agente/ponte.py:199`) publica a mensagem final num status `WORKING` e depois no estado terminal. Assim ela fica no `history` uma única vez e em `status.message`, como no exemplo `exemplos/wire/10-a2a-send-message-continuacao.json`.
4. **Ruído de log do `a2a-sdk`.** Em algumas execuções completas do validador, o agente registra `opentelemetry.context Failed to detach context` e `Task was destroyed but it is pending!`, os dois vindos da tarefa interna `EventQueueSource._dispatch_loop` do SDK. Isso aconteceu em Python 3.10, 3.11, 3.12 e 3.14, sempre logo depois da verificação 30. Não reproduziu em sequências isoladas, porque depende do momento em que o coletor de lixo roda. O efeito fica só no log: as 36 verificações passam do mesmo jeito.

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de um clone limpo:

```
trace-id desta execucao: 41e77be3646e145c4de25fcb4f723a3b
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
