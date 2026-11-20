"""Fase 6a - Verificação periódica: comparar as fontes com o arquivo permanente.

Esta etapa só OLHA e ESCREVE RELATÓRIOS na pasta de dados. Não toca no
armazenamento do Office e não restaura nada.

A cada execução respondemos a quatro perguntas:

1. **Fontes.** Quais pastas ``pivot.claude.ai`` existem agora? Alguma é nova?
   Alguma que já fotografamos sumiu? (Uma que some junto com outra que aparece
   costuma ser "o Office mudou de pasta".)
2. **Banco recriado.** A data de criação do banco mudou entre snapshots?
3. **Conversas ausentes.** Quais conversas do arquivo não estão em nenhuma
   fonte atual?
4. **Alertas.** Há algum alerta de perda ainda não visto?

"Perda detectada" = existe alerta aberto. Enquanto você não marca o alerta
como visto (``alerts --ack``), a verificação continua acusando; mas o relatório
datado, o plano de restauração e a notificação saem UMA vez por conjunto de
alertas, para não repetir o aviso a cada 15 minutos.

O plano de restauração é sempre uma SIMULAÇÃO: descreve o que cada estratégia
faria e o que se perderia, e não executa nada.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .archive import Alert, Archive
from .config import Config
from .conversation import Conversation
from .discovery import Source
from .export import conversation_dir
from .snapshot import list_snapshots

log = logging.getLogger(__name__)

_STATE_NAME = "verification.json"
LATEST_NAME = "ultima-verificacao"


@dataclass
class SourceState:
    source_id: str
    rel_path: str
    status: str  # "ok" | "nova" | "sumiu"
    db_created_at: str | None
    generations: int  # quantas datas de criação diferentes o banco já teve
    snapshots: int
    last_snapshot: str | None
    last_snapshot_at: str | None


@dataclass
class AbsentConversation:
    id: str
    title: str | None
    app: str
    messages: int
    user_id: str | None
    org_id: str | None
    updated_at: str | None
    missing_since: str | None
    last_source_id: str | None
    last_snapshot: str | None
    snapshot_available: bool  # a cópia bruta que a contém ainda existe?
    export_path: str | None


@dataclass
class Verification:
    checked_at: datetime
    sources: list[SourceState] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)  # (pasta antiga, pasta nova)
    absent: list[AbsentConversation] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)  # abertos
    new_alerts: list[Alert] = field(default_factory=list)  # criados nesta verificação
    stats: dict = field(default_factory=dict)
    target_identities: dict[str, list[tuple[str | None, str | None]]] = field(default_factory=dict)

    @property
    def loss(self) -> bool:
        return bool(self.alerts)

    @property
    def fingerprint(self) -> str:
        """Identifica o CONJUNTO de alertas abertos (para avisar uma vez só)."""
        ids = sorted(a.id for a in self.alerts if a.id is not None)
        return hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16] if ids else ""


def _local(value: str | datetime | None) -> str:
    if not value:
        return "desconhecida"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone().strftime("%d/%m/%Y %H:%M")


def verify(cfg: Config, archive: Archive, sources: list[Source],
           now: datetime | None = None) -> Verification:
    """Monta o retrato atual e registra alertas de fonte que sumiu."""
    now = now or datetime.now(timezone.utc)
    result = Verification(checked_at=now)
    discovered = {s.source_id: s for s in sources}

    # ---- 1) fontes: as de agora e as que já fotografamos
    known: dict[str, list] = {}
    if cfg.snapshots_dir.is_dir():
        for child in sorted(cfg.snapshots_dir.iterdir()):
            snaps = list_snapshots(child) if child.is_dir() else []
            if snaps:
                known[child.name] = snaps
    for source_id in sorted(set(discovered) | set(known)):
        snaps = known.get(source_id, [])
        source = discovered.get(source_id)
        generations = {s.manifest.get("db_created_at") for s in snaps} - {None}
        if source is None:
            status = "sumiu"
        elif len(snaps) <= 1 and archive.ingested_count(source_id) <= 1:
            status = "nova"
        else:
            status = "ok"
        last = snaps[-1] if snaps else None
        result.sources.append(SourceState(
            source_id=source_id,
            rel_path=source.rel_path if source else (last.manifest.get("source_rel_path") or "?"),
            status=status,
            db_created_at=last.manifest.get("db_created_at") if last else None,
            generations=len(generations), snapshots=len(snaps),
            last_snapshot=last.path.name if last else None,
            last_snapshot_at=last.manifest.get("taken_at") if last else None))
    gone = [s for s in result.sources if s.status == "sumiu"]
    new = [s for s in result.sources if s.status == "nova"]
    result.moved = [(g.rel_path, n.rel_path) for g in gone for n in new]

    # ---- 3) conversas do arquivo que não estão em nenhuma fonte atual
    for row in archive.conversations_outside(set(discovered)):
        conv = Conversation(id=row["id"], title=row["title"], surface=row["surface"],
                            created_at=datetime.fromisoformat(row["created_at"])
                            if row["created_at"] else None)
        folder = conversation_dir(cfg.exports_dir, conv)
        snapshot_dir = (cfg.snapshots_dir / (row["last_source_id"] or "") / (row["last_snapshot"] or "")
                        if row["last_source_id"] and row["last_snapshot"] else None)
        result.absent.append(AbsentConversation(
            id=row["id"], title=row["title"], app=conv.app, messages=row["active_messages"],
            user_id=row["user_id"], org_id=row["org_id"], updated_at=row["updated_at"],
            missing_since=row["missing_since"], last_source_id=row["last_source_id"],
            last_snapshot=row["last_snapshot"],
            snapshot_available=bool(snapshot_dir and (snapshot_dir / "manifest.json").is_file()),
            export_path=str(folder / "conversa.md") if (folder / "conversa.md").is_file() else None))

    # ---- alerta próprio desta etapa: fonte que sumiu levando conversas
    absent_by_source: dict[str, list[AbsentConversation]] = {}
    for item in result.absent:
        absent_by_source.setdefault(item.last_source_id or "", []).append(item)
    with archive.transaction():
        # Conversas já citadas em algum alerta não resolvido (mesmo que você já
        # o tenha marcado como visto): não avisamos de novo.
        covered: set[str] = set()
        for alert in archive.open_alerts(include_acknowledged=True):
            if alert.kind != "source_gone":
                covered.update(alert.details.get("absent_ids", []))
                covered.update(c.get("id") for c in alert.details.get("conversations", []))
        for state in result.sources:
            key = f"source-gone:{state.source_id}"
            orphans = [c for c in absent_by_source.get(state.source_id, []) if c.id not in covered]
            # Sem nenhuma fonte no disco, o alerta "no_source_found" já cobre o caso.
            if state.status == "sumiu" and orphans and discovered:
                alert = archive.add_alert(
                    Alert("source_gone",
                          f"Possível perda de histórico: a fonte {state.source_id} não existe mais "
                          f"no disco e {len(orphans)} conversa(s) dela não estão em nenhuma outra "
                          f"fonte. Elas continuam guardadas no arquivo.",
                          state.source_id,
                          {"conversations": [{"id": c.id, "title": c.title} for c in orphans]}),
                    key, now)
                if alert:
                    result.new_alerts.append(alert)
            elif state.status != "sumiu" or not orphans:
                archive.resolve_alerts("source_gone", now, dedup_key=key)
        result.alerts = archive.open_alerts()
    result.stats = archive.stats()

    # Identidade (usuário, organização) das conversas presentes em cada fonte
    # atual: usada no plano para dizer se userId/orgId precisariam de ajuste.
    for source_id in discovered:
        result.target_identities[source_id] = archive.identities_in_source(source_id)

    log.info("verificacao: fontes=%d novas=%d sumidas=%d ausentes=%d alertas_abertos=%d perda=%s",
             len(discovered), len(new), len(gone), len(result.absent), len(result.alerts),
             "sim" if result.loss else "nao")
    return result


# ----------------------------------------------------------------- relatório
_STATUS_LABEL = {"ok": "presente", "nova": "NOVA", "sumiu": "SUMIU"}


def render_report(v: Verification) -> str:
    lines = [
        "# Verificação do backup",
        "",
        f"- **Verificado em:** {_local(v.checked_at)}",
        f"- **Resultado:** " + ("**POSSÍVEL PERDA DE HISTÓRICO** (há alertas abertos)" if v.loss
                                else "sem sinais de perda"),
        f"- **Arquivo permanente:** {v.stats.get('conversations', 0)} conversa(s), "
        f"{v.stats.get('messages', 0)} mensagem(ns)",
        "",
        "> Este relatório só descreve o que foi encontrado. Nada foi alterado, e nada foi "
        "apagado do arquivo permanente.",
        "",
        "## Fontes",
        "",
        "| Situação | Fonte | Banco criado em | Snapshots | Último snapshot | Pasta |",
        "|---|---|---|---|---|---|",
    ]
    for s in v.sources:
        recreated = f" ({s.generations} versões do banco já vistas)" if s.generations > 1 else ""
        lines.append(f"| {_STATUS_LABEL[s.status]} | `{s.source_id}` | {_local(s.db_created_at)}{recreated} "
                     f"| {s.snapshots} | {_local(s.last_snapshot_at)} | `{s.rel_path}` |")
    if not v.sources:
        lines.append("| - | nenhuma fonte encontrada nem fotografada | | | | |")
    for old, new in v.moved:
        lines += ["", f"Possível mudança de pasta: `{old}` sumiu e `{new}` apareceu."]

    lines += ["", f"## Conversas do arquivo ausentes das fontes atuais ({len(v.absent)})", ""]
    if v.absent:
        lines += ["| Conversa | App | Mensagens | Ausente desde | Título | Cópia bruta |",
                  "|---|---|---|---|---|---|"]
        for c in v.absent:
            raw = f"`{c.last_source_id}/{c.last_snapshot}`" if c.snapshot_available else "não há mais"
            lines.append(f"| `{c.id}` | {c.app} | {c.messages} | {_local(c.missing_since)} "
                         f"| {(c.title or 'Sem título').replace('|', '/')} | {raw} |")
    else:
        lines.append("Nenhuma: tudo o que está no arquivo também está em alguma fonte atual.")

    lines += ["", f"## Alertas abertos ({len(v.alerts)})", ""]
    for a in v.alerts:
        lines.append(f"- **#{a.id}** ({_local(a.created_at)}): {a.message}")
    if not v.alerts:
        lines.append("Nenhum.")
    else:
        lines += ["", "Para marcar como visto: `office-chat-keeper alerts --ack <número>`."]
    return "\n".join(lines) + "\n"


def report_to_json(v: Verification) -> dict:
    return {
        "checked_at": v.checked_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "loss": v.loss,
        "archive": v.stats,
        "sources": [vars(s) for s in v.sources],
        "moved": [{"from": a, "to": b} for a, b in v.moved],
        "absent_conversations": [vars(c) for c in v.absent],
        "open_alerts": [{"id": a.id, "kind": a.kind, "created_at": a.created_at,
                         "source_id": a.source_id, "message": a.message} for a in v.alerts],
    }


# ------------------------------------------------ plano de restauração (simulação)
def render_restore_plan(v: Verification, cfg: Config) -> str:
    """Descreve o que cada estratégia faria. NÃO executa nada."""
    present = [s for s in v.sources if s.status != "sumiu"]
    lines = [
        "# Plano de restauração (simulação)",
        "",
        f"Gerado em {_local(v.checked_at)}.",
        "",
        "> **Isto é só um plano.** Nada foi alterado no armazenamento do Office, e a ferramenta "
        "nunca restaura sozinha. Cada passo abaixo só acontece se você o executar e confirmar.",
        "",
        f"## O que falta na origem ({len(v.absent)} conversa(s))",
        "",
    ]
    for c in v.absent:
        lines.append(f"- `{c.id}` · {c.app} · {c.messages} mensagens · "
                     f"{c.title or 'Sem título'}")
    if not v.absent:
        lines.append("Nada: não há conversa do arquivo ausente das fontes atuais.")

    lines += ["", "## Onde restaurar", ""]
    if not present:
        lines.append("Não há nenhuma fonte no disco agora. Abra o suplemento do Claude em um app "
                     "do Office, para o Office criar a pasta, e rode `office-chat-keeper run`.")
    for s in present:
        lines.append(f"- Fonte `{s.source_id}` · banco criado em {_local(s.db_created_at)} · "
                     f"`{s.rel_path}`")
    if len(present) > 1:
        lines.append("")
        lines.append("Há mais de uma fonte: a restauração exigirá escolher uma delas.")

    by_snapshot: dict[tuple[str, str], list[AbsentConversation]] = {}
    without_raw = []
    for c in v.absent:
        if c.snapshot_available:
            by_snapshot.setdefault((c.last_source_id or "", c.last_snapshot or ""), []).append(c)
        else:
            without_raw.append(c)

    lines += [
        "", "## Estratégia A: restaurar o snapshot bruto inteiro", "",
        "Troca TODO o banco `pivot.claude.ai` da fonte pelo conteúdo de um snapshot. "
        "Exige Excel, Word, PowerPoint e Outlook fechados.", "",
    ]
    if by_snapshot:
        for (source_id, snapshot), items in sorted(by_snapshot.items()):
            lines.append(f"- Snapshot `{source_id}/{snapshot}` contém {len(items)} das conversas "
                         f"que faltam.")
        lines += [
            "",
            "Consequências:",
            "- Conversas criadas ou alteradas na origem DEPOIS do snapshot saem do Office "
            "(continuam no arquivo permanente e nas exportações).",
            "- Skills e conectores voltam ao estado do snapshot, inclusive tokens antigos de "
            "conectores, que podem estar vencidos.",
            "- Antes de trocar, o estado atual é copiado para `pre-restore-<data>`; se a "
            "verificação posterior falhar, a troca é desfeita.",
        ]
    else:
        lines.append("Não aplicável: não há snapshot bruto disponível com as conversas que faltam.")

    lines += [
        "", "## Estratégia B: inserir só as conversas que faltam", "",
        "Um script, executado por você nas ferramentas de desenvolvedor do painel do Claude, "
        "grava com `add()` apenas as conversas ausentes, sem apagar nem alterar as existentes.", "",
    ]
    insertable = [c for items in by_snapshot.values() for c in items]
    if insertable:
        lines.append(f"- {len(insertable)} conversa(s) podem ser inseridas (o registro bruto "
                     f"completo está em um snapshot).")
        wanted = sorted({(c.user_id, c.org_id) for c in insertable}, key=str)
        for s in present:
            current = v.target_identities.get(s.source_id, [])
            if not current:
                lines.append(f"- Fonte `{s.source_id}`: está vazia, então a identidade atual "
                             f"(usuário e organização) é desconhecida. Abra uma conversa nova no "
                             f"suplemento e rode `run` antes, para a ferramenta saber se "
                             f"`userId`/`orgId` precisam de ajuste.")
            elif set(wanted) <= set(current):
                lines.append(f"- Fonte `{s.source_id}`: mesma identidade das conversas a inserir. "
                             f"`userId`/`orgId` NÃO seriam alterados.")
            else:
                lines.append(f"- Fonte `{s.source_id}`: identidade DIFERENTE. As conversas a "
                             f"inserir têm {wanted}; a fonte tem {sorted(current, key=str)}. "
                             f"`userId`/`orgId` seriam ajustados para a identidade atual, e isso "
                             f"seria mostrado antes de gravar.")
    else:
        lines.append("Não aplicável: sem o registro bruto não há o que inserir.")
    if without_raw:
        lines += ["", f"{len(without_raw)} conversa(s) não têm mais cópia bruta (o snapshot que as "
                      f"continha foi removido pela retenção). Para elas só vale o caminho seguro."]

    lines += [
        "", "## Caminho seguro (sempre disponível)", "",
        "Abrir uma conversa nova no suplemento e anexar o Markdown exportado, para o Claude "
        "retomar o contexto. Não escreve nada no armazenamento do Office.", "",
    ]
    for c in v.absent:
        lines.append(f"- `{c.id[:8]}`: " + (f"`{c.export_path}`" if c.export_path
                                            else "ainda não exportada; rode `office-chat-keeper export`"))
    lines += [
        "", "## Como executar", "",
        "- Estratégia A: `office-chat-keeper restore-snapshot <fonte>/<snapshot>` simula; "
        "acrescente `--execute` para trocar de verdade (pede confirmação digitada).",
        "- Estratégia B: `office-chat-keeper restore-script` gera o pacote; siga o `PASSOS.md`.",
        "- Detalhes e limites: `docs/restauracao.md` no repositório.", "",
        "A restauração dentro do suplemento não é suportada oficialmente e pode deixar de "
        "funcionar com uma atualização. Na dúvida, use o caminho seguro.",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ gravar em disco
@dataclass
class ReportOutcome:
    latest: Path
    new_loss: bool  # perda ainda não avisada (conjunto de alertas mudou)
    loss_report: Path | None = None
    plan: Path | None = None


def write_reports(cfg: Config, v: Verification) -> ReportOutcome:
    """Grava o relatório. Em perda NOVA, grava também relatório datado e plano."""
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    report = render_report(v)
    latest = cfg.reports_dir / f"{LATEST_NAME}.md"
    latest.write_text(report, encoding="utf-8")
    (cfg.reports_dir / f"{LATEST_NAME}.json").write_text(
        json.dumps(report_to_json(v), ensure_ascii=False, indent=2), encoding="utf-8")

    state_path = cfg.state_dir / _STATE_NAME
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8")).get("fingerprint", "")
    except (OSError, ValueError, AttributeError):
        previous = ""
    outcome = ReportOutcome(latest, new_loss=bool(v.loss and v.fingerprint != previous))
    if outcome.new_loss:
        stamp = v.checked_at.astimezone().strftime("%Y%m%d-%H%M%S")
        outcome.loss_report = cfg.reports_dir / f"perda-{stamp}.md"
        outcome.loss_report.write_text(report, encoding="utf-8")
        outcome.plan = cfg.reports_dir / f"plano-restauracao-{stamp}.md"
        outcome.plan.write_text(render_restore_plan(v, cfg), encoding="utf-8")
        log.warning("verificacao: PERDA nova; relatorio=%s plano=%s",
                    outcome.loss_report.name, outcome.plan.name)
    if v.fingerprint != previous:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"fingerprint": v.fingerprint}), encoding="utf-8")
    return outcome
