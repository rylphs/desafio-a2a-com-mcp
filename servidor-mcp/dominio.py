"""Domínio das salas: dados do starter, regras da política e cálculo de alternativas.

Tudo aqui é Python puro, sem nada de protocolo. As reservas vivem em memória:
as criadas durante a execução valem para as consultas seguintes do mesmo
processo e somem num restart, como o enunciado permite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path


FUSO_SAO_PAULO = timezone(timedelta(hours=-3))

class RegraVioladaError(Exception):
    """Pedido que fere uma regra de sala ou de política. A mensagem vai para o cliente."""

class IntervaloTempo:
    """Representa um intervalo de tempo semiaberto [inicio, fim)."""

    def __init__(self, inicio: str, fim: datetime):
        self.inicio = self._from_iso_str_to_data(inicio)
        self.fim = self._from_iso_str_to_data(fim)

    def _from_iso_str_to_data(self, valor: str) -> datetime:
        try:
            instante = datetime.fromisoformat(valor)
        except (TypeError, ValueError):
            raise RegraVioladaError(ERRO_DATA.format(valor=valor)) from None
        if instante.tzinfo is None:
            raise RegraVioladaError(ERRO_DATA.format(valor=valor))
        return instante.astimezone(FUSO_SAO_PAULO)

    def is_valid(self) -> bool:
        return self.inicio <= self.fim

    def exceeds(self, max_duration: timedelta) -> bool:
        return self.fim - self.inicio > max_duration
    
    def is_within(self, start: time, end: time) -> bool:
        return start <= self.inicio.time() <= end and start <= self.fim.time() <= end
    
    def colides(self, other: IntervaloTempo) -> bool:
        return other.inicio < self.fim and other.fim > self.inicio

@dataclass(frozen=True)
class Pedido:
    sala: dict
    intervalo: IntervaloTempo

@dataclass()
class Reserva:
    id: str
    sala: str
    intervalo: IntervaloTempo
    responsavel: str
    
    def __init__(self, id: str, sala: dict, inicio: datetime, fim: datetime, responsavel: str):
        self.id = id
        self.sala = sala
        self.intervalo = IntervaloTempo(inicio, fim)
        self.responsavel = responsavel
    
@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]

DADOS = Path(__file__).resolve().parent.parent / "dados"

HORARIO_ABERTURA = time(8, 0)
HORARIO_FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_CONFLITO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: {valor} nao esta em ISO 8601 com fuso horario"

POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


salas_json = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
reservas_json = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
salas = [Sala(**sala) for sala in salas_json]
reservas = [Reserva(**reserva) for reserva in reservas_json]

def listar_conflitos(pedido: Pedido) -> list[Reserva]:
    """Reservas que colidem com o intervalo pedido."""
    return [r for r in reservas if r.sala == pedido.sala.id and r.intervalo.colides(pedido.intervalo)]

def validar_reserva(sala_id: str, inicio: str, fim: str) -> Pedido:
    """Aplica as regras na ordem: sala, intervalo, janela de uso, duração."""
    sala = next((sala for sala in salas if sala.id == sala_id), None)
    if not sala:
        raise RegraVioladaError(ERRO_SALA.format(sala=sala_id))
    pedido = Pedido(sala, IntervaloTempo(inicio, fim))
    if not pedido.intervalo.is_valid():
        raise RegraVioladaError(ERRO_INTERVALO)
    
    if (not pedido.intervalo.is_within(HORARIO_ABERTURA, HORARIO_FECHAMENTO)):
        raise RegraVioladaError(ERRO_JANELA)
    if pedido.intervalo.exceeds(DURACAO_MAXIMA):
        raise RegraVioladaError(ERRO_DURACAO)
    
    return pedido

def obter_alternativas(pedido: Pedido) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por (capacidade, id), no máximo três."""
    candidatas = [
        sala
        for sala in salas
        if sala.id != pedido.sala.id
        and sala.capacidade >= pedido.sala.capacidade
        and not reservas.filter(
            lambda r: r.sala == sala.id and r.intervalo.colides(pedido.intervalo)
        )
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
    reserva = {
        "id": f"res-{len(reservas) + 1:04d}",
        "sala": sala_id,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    reservas.append(reserva)
    return reserva
