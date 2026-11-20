"""Fase 6b - Estratégia A: restaurar um snapshot bruto inteiro.

ESTE É O ÚNICO MÓDULO QUE ESCREVE NA PASTA DO OFFICE. Por isso ele é cercado de
travas, nunca roda sozinho (a tarefa agendada não o chama) e só age depois de
uma confirmação digitada por você.

O que a estratégia A faz
------------------------
Troca as pastas ``.leveldb`` e ``.blob`` de uma fonte pelo conteúdo de um
snapshot. É a restauração mais fiel (os arquivos voltam byte a byte), mas é
"tudo ou nada": o banco INTEIRO volta ao instante do snapshot, inclusive
skills e conectores, e conversas mais novas saem do Office (continuam no
arquivo permanente).

Por que não escrevemos dentro do LevelDB
----------------------------------------
Um banco LevelDB só é consistente como conjunto de arquivos. Alterar um
arquivo por conta própria corromperia o banco. Aqui nunca editamos arquivos:
trocamos a PASTA inteira por outra que já era um banco válido.

As travas, na ordem
-------------------
1. O snapshot é conferido contra o seu manifesto (SHA-256 de cada arquivo).
2. Excel, Word, PowerPoint e Outlook precisam estar fechados.
3. O banco precisa estar livre: abrimos o arquivo ``LOCK`` com acesso
   exclusivo; se qualquer processo o estiver usando, o Windows recusa.
4. O estado atual da fonte precisa já estar no backup (snapshot + arquivo).
5. O esquema do snapshot precisa ser igual ao da fonte. Única tolerância: um
   banco que existe só no snapshot (o suplemento cria alguns sob demanda) vira
   aviso na simulação, porque a troca o traz junto.
6. Você digita uma frase de confirmação.

A troca, passo a passo (e por que é reversível)
-----------------------------------------------
a. Copia o estado atual para ``pre-restore\\<fonte>-<data>`` e confere a cópia.
b. Copia o snapshot para uma pasta provisória AO LADO do destino e confere os
   hashes. Até aqui nada mudou no banco em uso.
c. RENOMEIA a pasta atual para ``...ock-antigo-<data>`` e a provisória para o
   nome definitivo. Renomear é instantâneo e reversível; e o Windows recusa
   renomear uma pasta que tenha arquivo aberto - mais uma proteção.
d. Relê o banco restaurado e compara as conversas com as do snapshot.
e. Se algo falhar em c ou d: apaga o que foi posto e renomeia o antigo de
   volta. Só depois de tudo conferido as pastas antigas são removidas (a
   cópia de segurança do passo a continua guardada).
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .archive import Archive
from .config import Config, is_inside
from .conversation import CHAT_DATABASE, CHATS_STORE
from .discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME, Source
from .protect import _NO_WINDOW, _system32
from .reader import SnapshotReader
from .snapshot import MANIFEST_NAME, fingerprint, list_snapshots, scan_source

log = logging.getLogger(__name__)

OFFICE_IMAGES = ("EXCEL.EXE", "WINWORD.EXE", "POWERPNT.EXE", "OUTLOOK.EXE", "OLK.EXE")
_CHUNK = 1024 * 1024


class RestoreError(Exception):
    """A restauração não pôde ser feita (ou foi desfeita)."""


# ------------------------------------------------------------------- travas
def running_office_apps() -> list[str]:
    """Nomes dos apps do Office em execução (via ``tasklist``, do Windows)."""
    if os.name != "nt":
        return []
    proc = subprocess.run([_system32("tasklist.exe"), "/FO", "CSV", "/NH"],
                          capture_output=True, creationflags=_NO_WINDOW)
    names = set()
    for row in csv.reader(proc.stdout.decode("oem", errors="replace").splitlines()):
        if row and row[0].upper() in OFFICE_IMAGES:
            names.add(row[0].upper())
    return sorted(names)


def storage_is_free(leveldb_dir: Path) -> bool:
    """True se nenhum processo está usando o banco.

    Tentamos abrir o arquivo ``LOCK`` com "compartilhamento zero": o Windows só
    permite se NINGUÉM mais tiver o arquivo aberto. O handle é fechado em
    seguida; nada é lido nem escrito. É mais preciso do que procurar processos
    ``msedgewebview2``, que existem às dezenas por causa de outros programas.
    """
    lock = Path(leveldb_dir) / "LOCK"
    if not lock.is_file():
        return True
    if os.name != "nt":
        return True
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, open_existing = 0x80000000, 3
    handle = kernel32.CreateFileW(str(lock), generic_read, 0, None, open_existing, 0, None)
    if handle == wintypes.HANDLE(-1).value or handle is None:
        return False
    kernel32.CloseHandle(handle)
    return True


# ----------------------------------------------------- origem da restauração
@dataclass
class RestoreSource:
    """Uma pasta restaurável: um snapshot ou uma cópia ``pre-restore``."""

    path: Path
    manifest: dict

    @property
    def leveldb(self) -> Path:
        return self.path / LEVELDB_DIR_NAME

    @property
    def blob(self) -> Path | None:
        blob = self.path / BLOB_DIR_NAME
        return blob if blob.is_dir() else None

    @property
    def label(self) -> str:
        return f"{self.path.parent.name}/{self.path.name}"


def resolve_restore_source(cfg: Config, spec: str) -> RestoreSource:
    """Aceita ``<fonte>/<snapshot>`` (dentro de snapshots) ou o caminho de uma pasta."""
    candidates = [cfg.snapshots_dir / spec, Path(spec)]
    for candidate in candidates:
        manifest_path = candidate / MANIFEST_NAME
        if manifest_path.is_file() and (candidate / LEVELDB_DIR_NAME).is_dir():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise RestoreError(f"O manifesto de {candidate} está ilegível.") from exc
            return RestoreSource(candidate.resolve(), manifest)
    raise RestoreError(f"Não achei um snapshot restaurável em '{spec}'. Use <fonte>/<snapshot> "
                       f"(veja 'office-chat-keeper restore-snapshot') ou o caminho de uma pasta "
                       f"pre-restore.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_problems(folder: Path, manifest: dict) -> list[str]:
    """Confere os arquivos de dados de ``folder`` contra o manifesto."""
    problems = []
    for entry in manifest.get("files", []):
        if entry.get("kind", "data") != "data":
            continue
        path = folder / entry["path"]
        if not path.is_file():
            problems.append(f"falta o arquivo {entry['path']}")
        elif path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            problems.append(f"o arquivo {entry['path']} não confere com o manifesto")
    return problems


def _read_chat_ids(leveldb_dir: Path, blob_dir: Path | None) -> set[str]:
    """Ids das conversas de um banco (só as chaves; não abre as conversas)."""
    with SnapshotReader(leveldb_dir, blob_dir) as reader:
        if CHATS_STORE not in reader.schema().get(CHAT_DATABASE, []):
            return set()
        return {key for key in reader.keys(CHAT_DATABASE, CHATS_STORE) if isinstance(key, str)}


def _read_schema(leveldb_dir: Path, blob_dir: Path | None) -> dict:
    with SnapshotReader(leveldb_dir, blob_dir) as reader:
        return reader.schema_details()


def schema_differences(wanted: dict, current: dict) -> tuple[list[str], list[str]]:
    """Diferenças de esquema entre o snapshot e a fonte: (impedem, só avisam).

    Um banco que existe só no snapshot apenas avisa: o suplemento cria alguns
    bancos sob demanda (um armazenamento novo ainda não os tem), e a troca leva
    o banco inteiro do snapshot, com o esquema dele. Qualquer outra diferença
    (versão, stores, índices, banco só na fonte) impede a restauração.
    """
    diffs, harmless = [], []
    for name in sorted(set(wanted) | set(current)):
        if name not in current:
            harmless.append(f"o banco '{name}' existe no snapshot e ainda não na fonte "
                            "(o suplemento o cria sob demanda); ele volta junto com o snapshot")
        elif name not in wanted:
            diffs.append(f"o banco '{name}' existe na fonte e não no snapshot")
        elif wanted[name] != current[name]:
            if wanted[name].get("version") != current[name].get("version"):
                diffs.append(f"banco '{name}': versão {wanted[name].get('version')} no snapshot, "
                             f"{current[name].get('version')} na fonte")
            else:
                diffs.append(f"banco '{name}': stores, chaves ou índices diferentes")
    return diffs, harmless


# -------------------------------------------------------------------- plano
@dataclass
class RestorePlan:
    source: RestoreSource
    target: Source
    snapshot_ids: set[str] = field(default_factory=set)
    present_ids: set[str] | None = None  # None = estado atual ainda não está no backup
    titles: dict[str, str | None] = field(default_factory=dict)
    cross_folder: bool = False
    target_fingerprint: str = ""
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def returning(self) -> list[str]:
        return sorted(self.snapshot_ids - (self.present_ids or set()))

    @property
    def leaving(self) -> list[str]:
        return sorted((self.present_ids or set()) - self.snapshot_ids)

    @property
    def staying(self) -> list[str]:
        return sorted(self.snapshot_ids & (self.present_ids or set()))


def build_plan(cfg: Config, archive: Archive, source: RestoreSource, target: Source) -> RestorePlan:
    """Simulação: calcula o que mudaria e o que impede a execução. Não altera nada."""
    plan = RestorePlan(source, target)
    plan.titles = {row["id"]: row["title"] for row in archive.summaries()}
    plan.cross_folder = source.manifest.get("source_id") != target.source_id

    if not source.manifest.get("stable", False):
        plan.blockers.append("o snapshot foi marcado como instável (a fonte mudou durante a cópia)")
    problems = manifest_problems(source.path, source.manifest)
    if problems:
        plan.blockers.append("o snapshot não está íntegro: " + "; ".join(problems[:3]))
    else:
        try:
            plan.snapshot_ids = _read_chat_ids(source.leveldb, source.blob)
        except Exception as exc:
            plan.blockers.append(f"não consegui ler o snapshot ({type(exc).__name__})")

    # O estado atual da fonte precisa estar fotografado E no arquivo permanente:
    # é a garantia de que nada do que está lá hoje se perde com a troca.
    plan.target_fingerprint = fingerprint(scan_source(target))
    snaps = list_snapshots(cfg.snapshots_dir / target.source_id)
    latest = snaps[-1] if snaps else None
    if (latest is None or not latest.stable
            or latest.manifest.get("fingerprint") != plan.target_fingerprint
            or not archive.is_ingested(target.source_id, latest.path.name)):
        plan.blockers.append("o estado atual da fonte ainda não está no backup; "
                             "rode 'office-chat-keeper run' antes")
    else:
        try:
            plan.present_ids = _read_chat_ids(latest.path / LEVELDB_DIR_NAME,
                                              latest.path / BLOB_DIR_NAME)
            if not problems:
                diffs, harmless = schema_differences(
                    _read_schema(source.leveldb, source.blob),
                    _read_schema(latest.path / LEVELDB_DIR_NAME, latest.path / BLOB_DIR_NAME))
                plan.warnings += harmless
                if diffs:
                    plan.blockers.append("o esquema do snapshot difere do da fonte: "
                                         + "; ".join(diffs[:4]))
        except Exception as exc:
            plan.blockers.append(f"não consegui ler o estado atual da fonte ({type(exc).__name__})")

    apps = running_office_apps()
    if apps:
        plan.blockers.append("feche antes: " + ", ".join(apps))
    if not storage_is_free(target.leveldb_dir):
        plan.blockers.append("o banco da fonte está em uso por algum processo")
    if plan.cross_folder:
        plan.warnings.append("o snapshot veio de OUTRA pasta de armazenamento; o suplemento pode "
                             "não reconhecer as conversas se a identidade for diferente")
    return plan


def render_plan(plan: RestorePlan) -> str:
    def listing(ids: list[str]) -> list[str]:
        return [f"      - {i[:8]}  {plan.titles.get(i) or 'Sem título'}" for i in ids[:15]] + (
            [f"      ... e mais {len(ids) - 15}"] if len(ids) > 15 else [])

    lines = [
        "PLANO DE RESTAURAÇÃO (estratégia A: snapshot bruto inteiro)",
        f"  snapshot:  {plan.source.label}  (tirado em "
        f"{_fmt(plan.source.manifest.get('taken_at'))})",
        f"  destino:   fonte {plan.target.source_id}",
        f"             {plan.target.leveldb_dir.parent}",
        f"  conversas no snapshot: {len(plan.snapshot_ids)}",
    ]
    if plan.present_ids is not None:
        lines.append(f"  conversas na fonte hoje: {len(plan.present_ids)}")
        lines.append(f"  VOLTAM ao Office ({len(plan.returning)}):")
        lines += listing(plan.returning)
        lines.append(f"  SAEM do Office, mas continuam no arquivo permanente ({len(plan.leaving)}):")
        lines += listing(plan.leaving)
        lines.append(f"  ficam, na versão que tinham no snapshot ({len(plan.staying)}):")
        lines += listing(plan.staying)
    lines.append("  Skills e conectores do suplemento também voltam ao estado do snapshot.")
    for warning in plan.warnings:
        lines.append(f"  AVISO: {warning}")
    if plan.blockers:
        lines.append("  NÃO PODE SER EXECUTADO AGORA:")
        lines += [f"      - {reason}" for reason in plan.blockers]
    else:
        lines.append("  Nenhum impedimento encontrado.")
    return "\n".join(lines)


def _fmt(value: str | None) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M:%S") if value else "?"


# ----------------------------------------------------------------- execução
@dataclass
class RestoreResult:
    backup_dir: Path
    restored_ids: set[str]
    report: Path | None = None


def unique_dir(path: Path) -> Path:
    """Devolve ``path`` ou, se já existir, ``path-2``, ``path-3``...

    Os nomes de pasta levam data e hora até o segundo. Em uma máquina rápida,
    duas operações podem cair no mesmo segundo; sem isto, a segunda falharia.
    """
    candidate, counter = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}-{counter}")
        counter += 1
    return candidate


def _copy_tree_with_manifest(source: Source, dest: Path, now: datetime) -> dict:
    """Cópia de segurança do estado atual (mesmo formato de um snapshot)."""
    dest.mkdir(parents=True, exist_ok=False)
    entries = scan_source(source)
    files = []
    for entry in entries:
        target = dest / entry.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "lock":
            target.touch()
            continue
        shutil.copyfile(entry.path, target)
        os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))
        files.append({"path": entry.rel, "size": target.stat().st_size, "mtime_ns": entry.mtime_ns,
                      "sha256": _sha256(target), "kind": entry.kind, "linked": False})
    (dest / LEVELDB_DIR_NAME).mkdir(exist_ok=True)
    manifest = {
        "format": 1, "tool_version": __version__, "kind": "pre-restore",
        "source_id": source.source_id, "source_rel_path": source.rel_path,
        "source_leveldb_dir": str(source.leveldb_dir),
        "taken_at": now.isoformat(timespec="seconds"),
        "fingerprint": fingerprint(entries), "stable": True,
        "file_count": len(files), "total_bytes": sum(f["size"] for f in files), "files": files,
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    return manifest


def execute_restore(cfg: Config, plan: RestorePlan, *, now: datetime | None = None) -> RestoreResult:
    """Executa a troca. Chame só depois da confirmação explícita do usuário."""
    now = now or datetime.now().astimezone()
    target = plan.target
    if plan.blockers:
        raise RestoreError("Há impedimentos: " + "; ".join(plan.blockers))
    # As condições podem ter mudado entre o plano e a confirmação: confere de novo.
    apps = running_office_apps()
    if apps:
        raise RestoreError("Feche antes: " + ", ".join(apps))
    if not storage_is_free(target.leveldb_dir):
        raise RestoreError("O banco da fonte está em uso por algum processo.")
    if fingerprint(scan_source(target)) != plan.target_fingerprint:
        raise RestoreError("A fonte mudou depois do plano. Rode 'run' e refaça o plano.")

    stamp = now.strftime("%Y%m%d-%H%M%S")
    parent = target.leveldb_dir.parent
    live = {"leveldb": parent / LEVELDB_DIR_NAME, "blob": parent / BLOB_DIR_NAME}
    staged = {k: v.with_name(f"{v.name}.ock-novo-{stamp}") for k, v in live.items()}
    retired = {k: v.with_name(f"{v.name}.ock-antigo-{stamp}") for k, v in live.items()}

    # a) cópia de segurança do estado atual, fora da pasta do Office
    # (unique_dir: duas restaurações no mesmo segundo não podem disputar a mesma pasta)
    backup_dir = unique_dir(cfg.data_dir / "pre-restore" / f"{target.source_id}-{stamp}")
    backup_manifest = _copy_tree_with_manifest(target, backup_dir, now)
    if manifest_problems(backup_dir, backup_manifest) \
            or fingerprint(scan_source(target)) != plan.target_fingerprint:
        raise RestoreError("A cópia de segurança não conferiu; nada foi alterado.")
    log.info("restauracao: copia de seguranca fonte=%s pasta=%s arquivos=%d",
             target.source_id, backup_dir.name, backup_manifest["file_count"])

    placed: set[str] = set()  # pastas do snapshot já colocadas no lugar definitivo

    def rollback() -> None:
        """Desfaz a troca: tira o que foi posto e devolve o que havia."""
        for key in ("leveldb", "blob"):
            if key in placed:
                shutil.rmtree(live[key])
            if retired[key].exists():
                os.rename(retired[key], live[key])
            shutil.rmtree(staged[key], ignore_errors=True)

    try:
        # b) snapshot copiado para o lado do destino e conferido (o banco em uso segue intacto)
        shutil.copytree(plan.source.leveldb, staged["leveldb"])
        if plan.source.blob is not None:
            shutil.copytree(plan.source.blob, staged["blob"])
        for entry in plan.source.manifest.get("files", []):
            if entry.get("kind", "data") != "data":
                continue
            folder, _, rest = entry["path"].partition("/")
            copy = staged["leveldb" if folder == LEVELDB_DIR_NAME else "blob"] / rest
            if not copy.is_file() or _sha256(copy) != entry["sha256"]:
                raise RestoreError(f"A cópia de {entry['path']} não conferiu.")

        # c) a troca: renomear é instantâneo, reversível, e falha se houver arquivo aberto
        for key in ("leveldb", "blob"):
            if live[key].exists():
                os.rename(live[key], retired[key])
        for key in ("leveldb", "blob"):
            if staged[key].exists():
                os.rename(staged[key], live[key])
                placed.add(key)

        # d) reler e comparar com o que o snapshot tinha
        restored_ids = _read_chat_ids(live["leveldb"], live["blob"] if live["blob"].is_dir() else None)
        if restored_ids != plan.snapshot_ids:
            raise RestoreError(f"Depois da troca o banco tem {len(restored_ids)} conversa(s); "
                               f"o snapshot tinha {len(plan.snapshot_ids)}.")
    except BaseException as exc:
        try:
            rollback()
        except OSError as undo_error:  # o pior caso: orienta a recuperação manual
            log.error("restauracao: FALHA AO DESFAZER (%s)", type(undo_error).__name__)
            raise RestoreError(
                f"A restauração falhou ({exc}) E não consegui desfazer sozinho. O estado anterior "
                f"está em {backup_dir} e em {retired['leveldb']}.") from exc
        log.error("restauracao: falhou e foi desfeita (%s)", type(exc).__name__)
        if isinstance(exc, RestoreError):
            raise RestoreError(f"{exc} A troca foi DESFEITA; a fonte está como antes.") from exc
        raise RestoreError(f"A restauração falhou ({type(exc).__name__}: {exc}). A troca foi "
                           f"DESFEITA; a fonte está como antes.") from exc

    # e) tudo conferido: remove as pastas antigas (a cópia de segurança fica)
    for key in ("leveldb", "blob"):
        if retired[key].exists() and is_inside(retired[key], parent) \
                and ".ock-antigo-" in retired[key].name:
            shutil.rmtree(retired[key], ignore_errors=True)

    # Avisa o backup: o próximo snapshot desta fonte vai ter o banco "recriado"
    # por causa desta restauração, e isso não é sinal de perda.
    from .pipeline import mark_restored

    mark_restored(cfg, target.source_id, now)

    result = RestoreResult(backup_dir, restored_ids)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    result.report = cfg.reports_dir / f"restauracao-{stamp}.md"
    result.report.write_text("\n".join([
        "# Restauração executada (estratégia A)", "",
        f"- **Quando:** {_fmt(now.isoformat())}",
        f"- **Snapshot restaurado:** `{plan.source.label}`",
        f"- **Destino:** fonte `{target.source_id}` em `{parent}`",
        f"- **Conversas no banco depois da troca:** {len(restored_ids)} (conferido com o snapshot)",
        f"- **Voltaram:** {len(plan.returning)} · **Saíram do Office:** {len(plan.leaving)}",
        f"- **Cópia de segurança do estado anterior:** `{backup_dir}`", "",
        "## Para desfazer", "",
        "Com os apps do Office fechados:", "",
        f'    office-chat-keeper restore-snapshot "{backup_dir}" --target {target.source_id} --execute',
        "",
    ]), encoding="utf-8")
    log.info("restauracao: concluida fonte=%s snapshot=%s conversas=%d voltaram=%d sairam=%d",
             target.source_id, plan.source.label, len(restored_ids), len(plan.returning),
             len(plan.leaving))
    return result
