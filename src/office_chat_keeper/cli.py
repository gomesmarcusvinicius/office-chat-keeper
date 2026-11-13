"""Interface de linha de comando (CLI).

Comandos:

* ``status`` -> só olha: fontes, snapshots, arquivo permanente e alertas.
* ``run``    -> snapshot do que mudou, ingestão no arquivo permanente e
  exportação do que há de novo (Markdown, JSON, imagens).
* ``list``   -> lista as conversas guardadas no arquivo.
* ``search`` -> busca por termo no texto das mensagens.
* ``export`` -> refaz a exportação de todas as conversas do arquivo.
* ``alerts`` -> mostra os alertas e permite marcá-los como vistos.
* ``verify`` -> compara as fontes com o arquivo e grava um relatório (o
  ``run`` já faz isso a cada execução).
* ``install-task`` / ``uninstall-task`` -> liga e desliga o backup automático
  (tarefa no Agendador de Tarefas do Windows, sem administrador).
* ``restore-snapshot`` -> ÚNICO comando que escreve na pasta do Office. Sem
  ``--execute`` só simula; com ``--execute`` exige uma confirmação digitada.
* ``restore-script`` -> gera, na pasta de dados, o pacote da restauração
  seletiva (arquivo de dados + script para o DevTools + página de ensaio).
  Não grava nada no Office: quem executa o script é você.

``status``, ``list`` e ``search`` não alteram conversas, snapshots nem
exportações.

Opções globais (vêm ANTES do comando):
    office-chat-keeper --data-dir D:\\Backup --source-root C:\\outra\\pasta status
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import banner
from .config import (Config, ConfigError, check_destination_is_safe, load_config,
                     onedrive_root_for)
from .discovery import discover_sources
from .protect import acl_status, ensure_protected
from .snapshot import fingerprint, list_snapshots, scan_source, snapshot_source

log = logging.getLogger("office_chat_keeper")


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _fmt_epoch(epoch: float | None) -> str:
    if epoch is None:
        return "desconhecida"
    return datetime.fromtimestamp(epoch).strftime("%d/%m/%Y %H:%M:%S")


def _fmt_iso(value: str | None) -> str:
    if not value:
        return "desconhecida"
    # astimezone(): mostra na hora local, venha o texto em UTC ou com fuso.
    return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M:%S")


def _setup_logging(cfg: Config) -> None:
    """Log em arquivo, com rodízio (1 MB x 3 arquivos).

    Regra do projeto: o log só leva contagens, ids e nomes de pasta.
    Nunca texto de conversa.
    """
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(cfg.logs_dir / "office-chat-keeper.log",
                                  maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("office_chat_keeper")
    root.setLevel(logging.INFO)
    for old in list(root.handlers):  # evita linhas duplicadas se chamado 2x
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)


def _onedrive_warning(cfg: Config) -> str | None:
    root = onedrive_root_for(cfg.data_dir)
    if root is None:
        return None
    return (f"AVISO: a pasta de dados fica dentro do OneDrive ({root.name}). "
            "Os snapshots contêm tokens de conectores e as conversas contêm dados "
            "de trabalho; sincronizá-los pode ferir a política da sua empresa.")


def cmd_status(cfg: Config) -> int:
    print(banner())
    print()
    print(f"Origem (somente leitura): {cfg.source_root}")
    print(f"Pasta de dados:           {cfg.data_dir}"
          + ("" if cfg.data_dir.exists() else "  (ainda não existe; 'run' cria)"))
    marker = acl_status(cfg.state_dir)
    if marker is None:
        print("Proteção da pasta (ACL):  ainda não aplicada")
    elif marker.get("ok"):
        print(f"Proteção da pasta (ACL):  restrita ao seu usuário desde {_fmt_iso(marker.get('applied_at'))}")
    else:
        print(f"Proteção da pasta (ACL):  FALHOU ({marker.get('detail')})")
    warning = _onedrive_warning(cfg)
    if warning:
        print(warning)
    print()

    sources = discover_sources(cfg.source_root, exclude=(cfg.snapshots_dir,))
    if not cfg.source_root.is_dir():
        print("A pasta de origem não existe neste computador.")
    print(f"Fontes pivot.claude.ai encontradas: {len(sources)}")
    known_ids = set()
    for src in sources:
        known_ids.add(src.source_id)
        entries = scan_source(src)
        data = [e for e in entries if e.kind == "data"]
        snaps = list_snapshots(cfg.snapshots_dir / src.source_id)
        print(f"\n  [{src.source_id}] {src.rel_path}")
        print(f"    banco criado em:   {_fmt_epoch(src.db_created_at)}")
        print(f"    arquivos de dados: {len(data)} ({_human_size(sum(e.size for e in data))})"
              + ("" if src.blob_dir else "  [sem pasta .blob]"))
        if not snaps:
            print("    snapshots:         nenhum ainda")
            continue
        last = snaps[-1]
        changed = last.manifest.get("fingerprint") != fingerprint(entries) or not last.stable
        print(f"    snapshots:         {len(snaps)} (último em {_fmt_iso(last.manifest.get('taken_at'))}"
              + ("" if last.stable else ", INSTÁVEL") + ")")
        print("    desde o último:    " + ("a fonte MUDOU (o próximo 'run' copia)" if changed
                                            else "sem mudanças"))
        if _fmt_iso(last.manifest.get("db_created_at")) != _fmt_epoch(src.db_created_at):
            print("    ATENÇÃO: a data de criação do banco é diferente da do último snapshot "
                  "(o banco pode ter sido recriado).")

    # Fontes que já fotografamos mas não existem mais no disco.
    if cfg.snapshots_dir.is_dir():
        for child in sorted(cfg.snapshots_dir.iterdir()):
            if child.is_dir() and child.name not in known_ids:
                snaps = list_snapshots(child)
                if snaps:
                    last = snaps[-1]
                    print(f"\n  [{child.name}] FONTE NÃO ENCONTRADA AGORA "
                          f"({last.manifest.get('source_rel_path')})")
                    print(f"    snapshots guardados: {len(snaps)} "
                          f"(último em {_fmt_iso(last.manifest.get('taken_at'))})")

    print()
    newest = None
    if cfg.snapshots_dir.is_dir():
        for child in cfg.snapshots_dir.iterdir():
            snaps = list_snapshots(child) if child.is_dir() else []
            if snaps and (newest is None or snaps[-1].taken_at > newest):
                newest = snaps[-1].taken_at
    print("Último snapshot:    " + (newest.astimezone().strftime("%d/%m/%Y %H:%M:%S")
                                    if newest else "nenhum ainda"))
    _print_task_status(cfg)
    try:
        import json

        last = json.loads((cfg.reports_dir / "ultima-verificacao.json").read_text(encoding="utf-8"))
        print(f"Última verificação: {_fmt_iso(last.get('checked_at'))} ("
              + ("POSSÍVEL PERDA" if last.get("loss") else "sem sinais de perda") + ")")
    except (OSError, ValueError):
        pass
    if not cfg.archive_path.exists():
        print("Arquivo permanente: ainda não existe ('run' cria)")
        return 0
    from .archive import Archive

    with Archive(cfg.archive_path) as archive:
        stats = archive.stats()
        alerts = archive.open_alerts()
    print(f"Arquivo permanente: {stats['conversations']} conversa(s), {stats['messages']} "
          f"mensagem(ns), {stats['blobs']} imagem(ns)/anexo(s) ({_human_size(stats['blob_bytes'])})")
    print(f"    última ingestão:   {_fmt_iso(stats['last_ingest'])}")
    if stats["missing"]:
        print(f"    {stats['missing']} conversa(s) guardada(s) que não aparecem mais na origem")
    _print_alerts(alerts, heading=True)
    return 0


def _print_task_status(cfg: Config) -> None:
    from .scheduler import query_task

    try:
        info = query_task(cfg)
    except Exception:  # o status nunca deve falhar por causa do agendador
        info = None
    if info is None:
        print("Backup automático:  desligado (para ligar: office-chat-keeper install-task)")
        return
    every = f", a cada {info.interval_minutes} min" if info.interval_minutes else ""
    print(f"Backup automático:  ligado (tarefa {info.task_name}{every})")
    print(f"    última execução:   {info.last_run} ({info.result_hint})")
    print(f"    próxima execução:  {info.next_run}")


def cmd_install_task(cfg: Config, *, interval: int, task_name: str | None, run_now: bool,
                     include_source_root: bool) -> int:
    from .scheduler import SchedulerError, default_task_name, install_task

    ensure_protected(cfg.data_dir, cfg.state_dir)
    _setup_logging(cfg)
    try:
        result = install_task(cfg, interval_minutes=interval, task_name=task_name,
                              include_source_root=include_source_root, run_now=run_now)
    except (SchedulerError, OSError, RuntimeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        log.error("tarefa agendada: instalacao falhou (%s)", type(exc).__name__)
        return 1
    when = ("ao fazer logon e " if result.logon_trigger else "") + \
        f"a cada {result.interval_minutes} minutos"
    print(f"Backup automático ligado. Tarefa: {result.task_name}")
    print(f"    quando:    {when}")
    print(f"    comando:   {result.command} {result.arguments}")
    print("    conta:     a sua, sem privilégio de administrador, só com você logado")
    print(f"    definição: {result.xml_path}")
    if not result.logon_trigger:
        print("    AVISO: o Windows não aceitou o gatilho de logon; a tarefa roda só pelo intervalo.")
    if not result.windowless:
        print("    AVISO: pythonw.exe não foi encontrado; uma janela pode piscar a cada execução.")
    if result.started:
        print("A primeira execução começou agora, em segundo plano. "
              "Acompanhe com 'office-chat-keeper status'.")
    # Quando o nome não é o padrão, a dica de desligamento precisa lembrar o nome
    # usado, senão o usuário remove a tarefa errada.
    uninstall_hint = "office-chat-keeper uninstall-task"
    if result.task_name != default_task_name():
        uninstall_hint += f" --task-name {result.task_name}"
    print(f"Para desligar: {uninstall_hint}")
    log.info("tarefa agendada instalada: nome=%s intervalo=%d logon=%s sem_janela=%s",
             result.task_name, result.interval_minutes, result.logon_trigger, result.windowless)
    return 0


def cmd_uninstall_task(cfg: Config, task_name: str | None) -> int:
    from .scheduler import SchedulerError, installed_task_name, uninstall_task

    name = task_name or installed_task_name(cfg)
    try:
        removed = uninstall_task(name, cfg)
    except (SchedulerError, OSError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(f"Backup automático desligado (tarefa {name} removida).")
        if cfg.logs_dir.is_dir():
            _setup_logging(cfg)
            log.info("tarefa agendada removida: nome=%s", name)
    else:
        print(f"Não existe tarefa agendada com o nome {name}. Nada a fazer.")
    print(f"Seus dados continuam em {cfg.data_dir}.")
    return 0


def _ask_confirmation(phrase: str) -> bool:
    """Pede que o usuário digite a frase exata. Sem terminal interativo, recusa.

    Não existe opção para pular esta pergunta: a restauração é sempre manual.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        print("A restauração exige um terminal interativo: a confirmação é digitada por você.")
        return False
    print(f"Para confirmar, digite exatamente: {phrase}")
    try:
        typed = input("> ")
    except EOFError:
        return False
    return typed.strip() == phrase


def _list_restore_sources(cfg: Config, archive) -> None:
    from .snapshot import list_snapshots as _list

    ingested = archive.ingested_snapshots()
    pinned = archive.pinned_snapshots()
    print("Snapshots que podem ser restaurados (do mais antigo ao mais novo):")
    found = False
    if cfg.snapshots_dir.is_dir():
        for source_dir in sorted(p for p in cfg.snapshots_dir.iterdir() if p.is_dir()):
            for snap in _list(source_dir):
                found = True
                row = ingested.get((source_dir.name, snap.path.name))
                count = row["conversations"] if row is not None and row["conversations"] is not None else "?"
                marks = ("" if snap.stable else "  [instável]") + (
                    "  [última cópia bruta de alguma conversa]"
                    if snap.path.name in pinned.get(source_dir.name, set()) else "")
                print(f"  {source_dir.name}/{snap.path.name}   {_fmt_iso(snap.manifest.get('taken_at'))}"
                      f"   {count} conversa(s){marks}")
    backups = cfg.data_dir / "pre-restore"
    if backups.is_dir():
        for folder in sorted(p for p in backups.iterdir() if (p / "manifest.json").is_file()):
            found = True
            print(f"  {folder}   [cópia de segurança de antes de uma restauração]")
    if not found:
        print("  nenhum ainda. Rode 'office-chat-keeper run' primeiro.")
    print("\nPara simular: office-chat-keeper restore-snapshot <fonte>/<snapshot>")
    print("A simulação não altera nada. A execução (--execute) exige os apps do Office fechados "
          "e uma confirmação digitada.")


def cmd_restore_snapshot(cfg: Config, spec: str | None, target_id: str | None, execute: bool) -> int:
    from .restore import (RestoreError, build_plan, execute_restore, render_plan,
                          resolve_restore_source)

    archive = _open_archive(cfg)
    if archive is None:
        return 0
    with archive:
        if not spec:
            _list_restore_sources(cfg, archive)
            return 0
        try:
            source = resolve_restore_source(cfg, spec)
        except RestoreError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        sources = {s.source_id: s
                   for s in discover_sources(cfg.source_root, exclude=(cfg.snapshots_dir,))}
        wanted = target_id or source.manifest.get("source_id")
        target = sources.get(wanted)
        if target is None:
            print(f"ERRO: a fonte de destino '{wanted}' não existe no disco agora.", file=sys.stderr)
            if sources:
                print("Fontes existentes: " + ", ".join(sorted(sources))
                      + ". Para restaurar em outra pasta, indique-a com --target <id>.",
                      file=sys.stderr)
            else:
                print("Não há nenhuma fonte no disco. Abra o suplemento do Claude em um app do "
                      "Office para a pasta ser criada, feche o app e rode 'run'.", file=sys.stderr)
            return 2
        plan = build_plan(cfg, archive, source, target)
    print(render_plan(plan))
    if not execute:
        print("\nIsto foi uma SIMULAÇÃO: nada foi alterado. Para executar, acrescente --execute.")
        return 0
    if plan.blockers:
        print("\nNada foi alterado.")
        return 1
    _setup_logging(cfg)
    print()
    if not _ask_confirmation(f"RESTAURAR {target.source_id}"):
        print("Cancelado. Nada foi alterado.")
        return 1
    try:
        result = execute_restore(cfg, plan)
    except RestoreError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    print(f"Restauração concluída e conferida: {len(result.restored_ids)} conversa(s) no banco.")
    print(f"    cópia de segurança do estado anterior: {result.backup_dir}")
    print(f"    registro e instrução para desfazer:    {result.report}")
    print("Abra o suplemento e confira. Depois rode 'office-chat-keeper run'. Se alguma conversa "
          "saiu do Office com a troca, ele vai avisar que ela está ausente; isso é esperado, e "
          "ela continua no arquivo permanente.")
    return 0


def cmd_restore_script(cfg: Config, target_id: str | None, only: list[str] | None) -> int:
    from .restore_script import (PAYLOAD_NAME, STEPS_NAME, RestoreScriptError, create_bundle)

    archive = _open_archive(cfg)
    if archive is None:
        return 0
    sources = {s.source_id: s for s in discover_sources(cfg.source_root, exclude=(cfg.snapshots_dir,))}
    if target_id:
        target = sources.get(target_id)
    else:
        target = next(iter(sources.values())) if len(sources) == 1 else None
    if target is None:
        with archive:
            pass
        if not sources:
            print("ERRO: não há nenhuma fonte no disco. Abra o suplemento do Claude em um app do "
                  "Office para a pasta ser criada e rode 'run'.", file=sys.stderr)
        else:
            print("ERRO: indique a fonte de destino com --target <id>. Fontes existentes: "
                  + ", ".join(sorted(sources)), file=sys.stderr)
        return 2
    _setup_logging(cfg)
    with archive:
        try:
            bundle = create_bundle(cfg, archive, target, only=only)
        except RestoreScriptError as exc:
            print(str(exc))
            return 1
    print("Pacote de restauração seletiva gerado. NADA foi gravado no Office.")
    print(f"    pasta: {bundle.folder}")
    print(f"    {PAYLOAD_NAME}: {_human_size(bundle.payload_bytes)}, "
          f"esquema versão {bundle.schema_version}")
    print(f"Conversas no pacote ({len(bundle.included)}):")
    for item in bundle.included:
        note = "  [já está na fonte: o script vai pular]" if item.present else ""
        print(f"    - {item.id[:8]}  {item.messages} mensagens  {_clip(item.title or 'Sem título', 55)}{note}")
    for item, reason in bundle.skipped:
        print(f"    NÃO incluída: {item.id[:8]}  {_clip(item.title or 'Sem título', 40)}: {reason}")
    wanted = sorted({(c.user_id, c.org_id) for c in bundle.included}, key=str)
    if not bundle.target_identities:
        print("Identidade: a fonte de destino está vazia no backup, então não sei se userId/orgId "
              "precisariam de ajuste. O script decide ao ver o banco vivo e mostra na simulação.")
    elif set(wanted) <= set(bundle.target_identities):
        print("Identidade: a mesma da fonte de destino; userId/orgId não seriam alterados.")
    else:
        print("Identidade: DIFERENTE da fonte de destino; o script propõe ajustar userId/orgId "
              "e mostra isso na simulação.")
    print(f"\nPróximo passo: abra {bundle.folder / STEPS_NAME} e comece pelo ENSAIO "
          f"(não toca no suplemento).")
    return 0


def _print_alerts(alerts: list, *, heading: bool) -> None:
    if heading:
        print(f"Alertas abertos: {len(alerts)}")
    for alert in alerts:
        print(f"  ALERTA #{alert.id} ({_fmt_iso(alert.created_at)}): {alert.message}")
        for conv in alert.details.get("conversations", [])[:10]:
            print(f"      - {conv.get('id')}  {conv.get('title') or ''}")


def cmd_run(cfg: Config) -> int:
    ok, detail = ensure_protected(cfg.data_dir, cfg.state_dir)
    _setup_logging(cfg)
    from .lock import run_lock

    with run_lock(cfg.state_dir) as acquired:
        if not acquired:
            print("Outra execução do backup está em andamento; esta foi dispensada.")
            log.info("run: dispensado (outra execucao em andamento)")
            return 0
        try:
            return _run_backup(cfg, ok, detail)
        except Exception as exc:
            # Rodando agendado não há tela: o log é o único lugar onde o erro
            # aparece. Gravamos o tipo do erro e onde ele ocorreu (arquivo,
            # linha, função), sem a mensagem, que poderia conter trechos de
            # conversa.
            frames = " <- ".join(f"{Path(f.filename).name}:{f.lineno} ({f.name})"
                                 for f in reversed(traceback.extract_tb(exc.__traceback__)))
            log.error("run: falha inesperada %s em %s", type(exc).__name__, frames)
            print(f"ERRO inesperado ({type(exc).__name__}). Veja o log em {cfg.logs_dir}.",
                  file=sys.stderr)
            return 1


def _run_backup(cfg: Config, ok: bool, detail: str) -> int:
    if not ok:
        print(f"AVISO: não consegui restringir a permissão da pasta de dados ({detail}).")
    warning = _onedrive_warning(cfg)
    if warning:
        print(warning)
        log.warning("pasta de dados dentro do OneDrive")

    # Etapa 1: snapshot (cópia bruta) de cada fonte que mudou.
    sources = discover_sources(cfg.source_root, exclude=(cfg.snapshots_dir,))
    log.info("run: fontes=%d", len(sources))
    if not sources:
        print(f"Nenhuma fonte pivot.claude.ai encontrada em {cfg.source_root}.")
    from .archive import Archive

    # O arquivo sabe quais snapshots são a última cópia bruta de alguma
    # conversa; a retenção não pode apagá-los.
    pinned: dict[str, set[str]] = {}
    if cfg.archive_path.exists():
        with Archive(cfg.archive_path) as archive:
            pinned = archive.pinned_snapshots()
    failures = 0
    for src in sources:
        result = snapshot_source(src, cfg, protected=pinned.get(src.source_id, frozenset()))
        prefix = f"[{src.source_id}]"
        if result.status == "unchanged":
            print(f"{prefix} sem mudanças desde {_fmt_iso(result.manifest.get('taken_at'))}")
        elif result.status == "created":
            m = result.manifest
            extra = "" if m["stable"] else "  (INSTÁVEL: a fonte mudou durante a cópia)"
            print(f"{prefix} snapshot {result.path.name}: {m['file_count']} arquivos, "
                  f"{_human_size(m['total_bytes'])} ({_human_size(m['copied_bytes'])} novos){extra}")
            if result.deleted:
                print(f"{prefix} retenção: {result.deleted} snapshot(s) antigo(s) removido(s)")
        else:
            failures += 1
            print(f"{prefix} FALHA: {result.message}")

    # Etapas 2, 3 e 4: arquivo permanente, exportação e verificação.
    from .pipeline import alert_if_no_source, ingest_pending
    from .verify import verify, write_reports

    with Archive(cfg.archive_path) as archive:
        new_alerts = []
        no_source = alert_if_no_source(cfg, archive, len(sources))
        if no_source:
            new_alerts.append(no_source)
        for report in ingest_pending(cfg, archive):
            prefix = f"[{report.source_id}]"
            if report.error:
                failures += 1
                print(f"{prefix} FALHA ao ler o snapshot {report.snapshot}: {report.error}")
                continue
            print(f"{prefix} arquivo permanente <- snapshot {report.snapshot}: "
                  f"{len(report.conversations)} conversa(s) "
                  f"({report.count('new')} nova(s), {report.count('updated')} atualizada(s), "
                  f"{report.count('unchanged')} igual(is), {report.count('older')} antiga(s), "
                  f"{report.count('rejected')} rejeitada(s))")
            if report.unreadable:
                print(f"    AVISO: {report.unreadable} registro(s) não puderam ser lidos")
            for text in report.warnings:
                print(f"    AVISO: {text}")
            new_alerts += report.alerts
        _export(cfg, archive, force=False, include_thinking=False)
        check = verify(cfg, archive, sources)
        new_alerts += check.new_alerts
        open_alerts = check.alerts
    outcome = write_reports(cfg, check)

    if new_alerts:
        print()
        _print_alerts(new_alerts, heading=False)
        print("    Nada foi apagado do arquivo permanente nem das exportações.")
    if open_alerts:
        print(f"{len(open_alerts)} alerta(s) aberto(s). Veja 'office-chat-keeper alerts'.")
    else:
        print("verificação: sem sinais de perda")
    _announce_loss(outcome, len(open_alerts))
    return 1 if failures else 0


def _announce_loss(outcome, open_count: int) -> None:
    """Em perda NOVA: mostra onde estão o relatório e o plano, e notifica."""
    if not outcome.new_loss:
        return
    print(f"Relatório: {outcome.loss_report}")
    print(f"Plano de restauração (simulação; nada foi executado): {outcome.plan}")
    from .notify import notify

    notify("office-chat-keeper: possível perda de histórico",
           f"{open_count} alerta(s) aberto(s). Nada foi apagado do backup. "
           f"Rode: office-chat-keeper alerts")


def cmd_verify(cfg: Config, test_notification: bool) -> int:
    if test_notification:
        from .notify import notify

        accepted = notify("office-chat-keeper: teste",
                          "Se você está vendo este aviso, as notificações funcionam.",
                          warning=False)
        print("O Windows aceitou o pedido de notificação. Se o aviso não apareceu, verifique se "
              "as notificações estão ligadas e se o modo 'Não incomodar' está desligado."
              if accepted else "O Windows recusou o pedido de notificação.")
        return 0 if accepted else 1
    archive = _open_archive(cfg)
    if archive is None:
        return 0
    from .verify import verify, write_reports

    _setup_logging(cfg)
    sources = discover_sources(cfg.source_root, exclude=(cfg.snapshots_dir,))
    with archive:
        check = verify(cfg, archive, sources)
    outcome = write_reports(cfg, check)
    print("Resultado: " + ("POSSÍVEL PERDA DE HISTÓRICO (há alertas abertos)" if check.loss
                           else "sem sinais de perda"))
    labels = {"ok": "presente", "nova": "NOVA", "sumiu": "SUMIU"}
    for state in check.sources:
        print(f"  fonte [{state.source_id}] {labels[state.status]}: {state.snapshots} snapshot(s)"
              + (f", banco recriado {state.generations - 1} vez(es)" if state.generations > 1 else ""))
    for old, new in check.moved:
        print(f"  possível mudança de pasta: {old} -> {new}")
    print(f"  conversas do arquivo ausentes das fontes atuais: {len(check.absent)}")
    for item in check.absent[:20]:
        print(f"      - {item.id[:8]}  {item.app}  {_clip(item.title or 'Sem título', 60)}")
    _print_alerts(check.alerts, heading=True)
    print(f"Relatório completo: {outcome.latest}")
    _announce_loss(outcome, len(check.alerts))
    return 0


def _export(cfg: Config, archive, *, force: bool, include_thinking: bool) -> None:
    """Exporta do arquivo permanente o que mudou e imprime um resumo."""
    from .pipeline import export_changed

    results, extras = export_changed(cfg, archive, force=force, include_thinking=include_thinking)
    labels = {"written": "gravada", "unchanged": "sem mudança",
              "kept-larger": "MANTIDA A ANTERIOR (a nova viria com menos mensagens)"}
    if results:
        print(f"exportação: {len(results)} conversa(s)")
    for conv in results:
        try:
            shown = conv.path.relative_to(cfg.data_dir)
        except ValueError:
            shown = conv.path
        print(f"    {labels.get(conv.status, conv.status)}: {shown}")
        print(f"        {conv.messages} mensagens, {conv.images} imagens, {conv.attachments} anexos")
    if results or force:
        for database, count in extras.items():
            print(f"    {database}: {count} registro(s) exportado(s)")


def cmd_export(cfg: Config, include_thinking: bool) -> int:
    if not cfg.archive_path.exists():
        print("O arquivo permanente ainda não existe. Rode 'run' primeiro.")
        return 0
    ensure_protected(cfg.data_dir, cfg.state_dir)
    _setup_logging(cfg)
    from .archive import Archive

    with Archive(cfg.archive_path) as archive:
        if not archive.conversation_ids():
            print("O arquivo permanente está vazio. Rode 'run' primeiro.")
            return 0
        _export(cfg, archive, force=True, include_thinking=include_thinking)
    return 0


_KIND_LABELS = {"user": "Usuário", "assistant": "Claude", "thinking": "Raciocínio",
                "tool": "Ferramenta", "title": "Título"}


def _open_archive(cfg: Config):
    """Abre o arquivo permanente, ou devolve None (com aviso) se ainda não existe."""
    if not cfg.archive_path.exists():
        print("O arquivo permanente ainda não existe. Rode 'office-chat-keeper run' primeiro.")
        return None
    from .archive import Archive

    return Archive(cfg.archive_path)


def _app_name(surface: str | None) -> str:
    from .conversation import Conversation

    return Conversation(id="", surface=surface).app


def _clip(text: str | None, width: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def cmd_list(cfg: Config, app: str | None, limit: int | None) -> int:
    archive = _open_archive(cfg)
    if archive is None:
        return 0
    with archive:
        rows = archive.summaries()
    if app:
        wanted = app.lower()
        rows = [r for r in rows if wanted in (_app_name(r["surface"]).lower(),
                                              (r["surface"] or "").lower())]
    total = len(rows)
    if limit:
        rows = rows[:limit]
    if not rows:
        print("Nenhuma conversa no arquivo." if not app else f"Nenhuma conversa do app '{app}'.")
        return 0
    print(f"{'ATUALIZADA':<17} {'APP':<11} {'MSGS':>5}  {'ID':<8}  TÍTULO")
    for r in rows:
        flags = ""
        if r["missing_since"]:
            flags += "  [não está mais na origem]"
        if r["absent_messages"]:
            flags += f"  [+{r['absent_messages']} mensagem(ns) só no arquivo]"
        updated = _fmt_iso(r["updated_at"])[:16] if r["updated_at"] else "desconhecida"
        print(f"{updated:<17} {_clip(_app_name(r['surface']), 11):<11} {r['active_messages']:>5}  "
              f"{r['id'][:8]:<8}  {_clip(r['title'] or 'Sem título', 60)}{flags}")
    print(f"\n{total} conversa(s) no arquivo" + (f" (mostrando {len(rows)})" if len(rows) < total else ""))
    return 0


def cmd_search(cfg: Config, terms: list[str], *, phrase: bool, kinds: list[str] | None,
               conversation: str | None, limit: int) -> int:
    from .archive import ArchiveError
    from .conversation import Conversation
    from .export import conversation_dir, redact_secrets

    query = " ".join(terms).strip()
    if not query:
        print("Informe o termo: office-chat-keeper search \"termo\"")
        return 2
    archive = _open_archive(cfg)
    if archive is None:
        return 0
    with archive:
        try:
            hits, total = archive.search(query, phrase=phrase, kinds=kinds,
                                         conversation=conversation, limit=limit)
        except ArchiveError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        heads = {r["id"]: r for r in archive.summaries()}
    if not hits:
        print(f"Nenhum resultado para: {query}")
        return 0
    print(f"{total} resultado(s) para: {query}" + (f" (mostrando {len(hits)})" if len(hits) < total else ""))
    for number, hit in enumerate(hits, start=1):
        when = _fmt_iso(hit.timestamp)[:16] if hit.timestamp else "sem data"
        absent = "  [só no arquivo]" if hit.absent else ""
        print(f"\n[{number}] {when}  {_KIND_LABELS.get(hit.kind, hit.kind)}{absent}  ·  "
              f"{_clip(hit.title or 'Sem título', 50)}  ({hit.conversation_id[:8]})")
        print(f"    {_clip(redact_secrets(hit.snippet), 240)}")
        head = heads.get(hit.conversation_id)
        if head is not None:
            folder = conversation_dir(cfg.exports_dir, Conversation(
                id=head["id"], title=head["title"], surface=head["surface"],
                created_at=datetime.fromisoformat(head["created_at"]) if head["created_at"] else None))
            if (folder / "conversa.md").is_file():
                print(f"    {folder / 'conversa.md'}")
    return 0


def cmd_alerts(cfg: Config, ack: list[str] | None, show_all: bool) -> int:
    archive = _open_archive(cfg)
    if archive is None:
        return 0
    with archive:
        if ack:
            if "all" in ack or "todos" in ack:
                ids = None
            else:
                try:
                    ids = [int(a.lstrip("#")) for a in ack]
                except ValueError:
                    print("Use o número do alerta (ex.: --ack 3) ou --ack all.", file=sys.stderr)
                    return 2
            done = archive.acknowledge_alerts(ids, datetime.now(timezone.utc))
            print(f"{done} alerta(s) marcado(s) como visto(s). Nada foi apagado: eles continuam "
                  f"no histórico ('alerts --all').")
        if show_all:
            rows = archive.all_alerts()
            print(f"Histórico de alertas: {len(rows)}")
            for r in rows:
                state = ("resolvido em " + _fmt_iso(r["resolved_at"]) if r["resolved_at"]
                         else "visto em " + _fmt_iso(r["acknowledged_at"]) if r["acknowledged_at"]
                         else "ABERTO")
                print(f"  #{r['id']} ({_fmt_iso(r['created_at'])}) [{state}] {r['message']}")
        else:
            _print_alerts(archive.open_alerts(), heading=True)
            if not ack and archive.open_alerts():
                print("\nPara marcar como visto: office-chat-keeper alerts --ack <número>  (ou --ack all)")
    return 0


class _VersionAction(argparse.Action):
    """`--version`: mostra a versão e o aviso em UMA linha e encerra.

    A ação "version" pronta do argparse quebra o texto na largura do terminal;
    aqui a linha sai inteira.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(banner())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="office-chat-keeper",
        description="Backup local do histórico dos suplementos do Claude no Office "
                    "(ferramenta não oficial, experimental).")
    parser.add_argument("--version", action=_VersionAction,
                        help="mostra a versão e encerra")
    parser.add_argument("--data-dir", help="pasta de dados (padrão: %%USERPROFILE%%\\ClaudeOfficeBackup)")
    parser.add_argument("--source-root", help="pasta de origem a varrer (padrão: Wef\\webview2 do Office)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="comando")
    sub.add_parser("status", help="mostra fontes, snapshots, arquivo e alertas")
    sub.add_parser("run", help="snapshot + arquivo permanente + exportação do que for novo")
    listing = sub.add_parser("list", help="lista as conversas guardadas no arquivo permanente")
    listing.add_argument("--app", help="filtra por app (excel, word, powerpoint, outlook)")
    listing.add_argument("--limit", type=int, help="mostra só as N mais recentes")
    search = sub.add_parser(
        "search", help="busca por termo no texto das mensagens",
        description="Busca no arquivo permanente, sem diferenciar maiúsculas nem acentos. "
                    "Várias palavras: todas precisam aparecer na mensagem, em qualquer ordem. "
                    "Use * no fim de uma palavra para buscar por prefixo (concilia*).")
    search.add_argument("terms", nargs="+", metavar="termo", help="palavra(s) a buscar")
    search.add_argument("--phrase", action="store_true", help="busca a frase exata, na ordem digitada")
    search.add_argument("--kind", action="append", metavar="TIPO",
                        choices=["user", "assistant", "thinking", "tool", "title"],
                        help="restringe o tipo: user, assistant, thinking, tool, title (repetível)")
    search.add_argument("--conversation", metavar="ID", help="só nesta conversa (início do id)")
    search.add_argument("--limit", type=int, default=20, help="máximo de resultados (padrão: 20)")
    export = sub.add_parser("export", help="reexporta todas as conversas do arquivo permanente")
    export.add_argument("--include-thinking", action="store_true",
                        help="inclui os raciocínios do Claude no Markdown (recolhidos)")
    install = sub.add_parser(
        "install-task", help="liga o backup automático (Agendador de Tarefas)",
        description="Cria uma tarefa no Agendador de Tarefas do Windows, na sua conta e sem "
                    "administrador, que roda 'run' no logon e a cada N minutos, sem janela.")
    install.add_argument("--interval", type=int, default=15, metavar="MIN",
                         help="minutos entre execuções (padrão: 15; recomendado: 15 a 30)")
    install.add_argument("--task-name", help="nome da tarefa (padrão: office-chat-keeper-<usuário>)")
    install.add_argument("--no-run", action="store_true",
                         help="não dispara a primeira execução imediatamente")
    uninstall = sub.add_parser("uninstall-task", help="desliga o backup automático")
    uninstall.add_argument("--task-name", help="nome da tarefa, se foi instalada com outro nome")
    verify_cmd = sub.add_parser(
        "verify", help="compara as fontes com o arquivo e grava um relatório",
        description="Só lê e relata: não tira snapshot, não altera o Office e não restaura nada.")
    verify_cmd.add_argument("--test-notification", action="store_true",
                            help="mostra uma notificação de teste do Windows e sai")
    restore = sub.add_parser(
        "restore-snapshot", help="restaura um snapshot bruto inteiro (simula por padrão)",
        description="Estratégia A: troca o banco pivot.claude.ai de uma fonte pelo conteúdo de um "
                    "snapshot. Sem --execute, só mostra o que mudaria. Com --execute, exige os "
                    "apps do Office fechados e uma confirmação digitada; antes de trocar copia o "
                    "estado atual, depois relê e, se não conferir, desfaz.")
    restore.add_argument("snapshot", nargs="?",
                         help="<fonte>/<snapshot> ou o caminho de uma pasta pre-restore; "
                              "sem este argumento, lista os snapshots disponíveis")
    restore.add_argument("--target", metavar="ID",
                         help="fonte de destino (padrão: a mesma de onde o snapshot veio)")
    restore.add_argument("--execute", action="store_true",
                         help="executa de verdade, depois da confirmação digitada")
    script = sub.add_parser(
        "restore-script", help="gera o pacote da restauração seletiva (não grava no Office)",
        description="Estratégia B: prepara um arquivo de dados com as conversas que faltam na "
                    "fonte, um script para colar no DevTools do painel do Claude e uma página de "
                    "ensaio. O script só insere o que falta e nunca altera conversas existentes.")
    script.add_argument("--target", metavar="ID", help="fonte de destino (se houver mais de uma)")
    script.add_argument("--conversation", action="append", metavar="ID",
                        help="inclui esta conversa (início do id), mesmo que já esteja na fonte; "
                             "repetível. Sem isso, entram as que faltam na fonte")
    alerts = sub.add_parser("alerts", help="mostra os alertas de possível perda")
    alerts.add_argument("--ack", nargs="+", metavar="N",
                        help="marca como visto(s): números dos alertas, ou 'all'")
    alerts.add_argument("--all", action="store_true", dest="show_all",
                        help="mostra o histórico completo, inclusive vistos e resolvidos")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.data_dir, args.source_root)
    try:
        check_destination_is_safe(cfg)
    except ConfigError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "export":
        return cmd_export(cfg, args.include_thinking)
    if args.command == "list":
        return cmd_list(cfg, args.app, args.limit)
    if args.command == "search":
        return cmd_search(cfg, args.terms, phrase=args.phrase, kinds=args.kind,
                          conversation=args.conversation, limit=max(1, args.limit))
    if args.command == "alerts":
        return cmd_alerts(cfg, args.ack, args.show_all)
    if args.command == "verify":
        return cmd_verify(cfg, args.test_notification)
    if args.command == "restore-script":
        return cmd_restore_script(cfg, args.target, args.conversation)
    if args.command == "restore-snapshot":
        return cmd_restore_snapshot(cfg, args.snapshot, args.target, args.execute)
    if args.command == "install-task":
        return cmd_install_task(cfg, interval=args.interval, task_name=args.task_name,
                                run_now=not args.no_run,
                                include_source_root=args.source_root is not None)
    if args.command == "uninstall-task":
        return cmd_uninstall_task(cfg, args.task_name)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
