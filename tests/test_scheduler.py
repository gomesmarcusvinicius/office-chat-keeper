"""Testes do agendamento.

Estes testes NÃO criam tarefas de verdade: a chamada ao ``schtasks.exe`` é
trocada por um dublê que só anota o que seria executado. Há um único teste
real, opcional, ligado pela variável OFFICE_CHAT_KEEPER_TEST_SCHEDULER=1.
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from office_chat_keeper import cli, scheduler
from office_chat_keeper.config import Config
from office_chat_keeper.lock import run_lock
from office_chat_keeper.scheduler import (SchedulerError, build_run_arguments, build_task_xml,
                                          default_task_name, install_task, iso_duration,
                                          query_task, uninstall_task)

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "pasta de dados", source_root=tmp_path / "origem")


def parse(xml: str) -> ET.Element:
    # O ElementTree recusa a declaração "UTF-16" em texto já decodificado.
    return ET.fromstring(xml.split("?>", 1)[1])


def text(root: ET.Element, path: str) -> str | None:
    node = root.find(path, NS)
    return node.text if node is not None else None


# ------------------------------------------------------------ definição
def test_duracao_iso():
    assert iso_duration(15) == "PT15M"
    assert iso_duration(60) == "PT1H"
    assert iso_duration(90) == "PT1H30M"


def test_argumentos_levam_a_pasta_de_dados_com_aspas(cfg):
    args = build_run_arguments(cfg, include_source_root=False)
    assert args.startswith("-P -m office_chat_keeper --data-dir ")
    assert f'"{cfg.data_dir.resolve()}"' in args  # caminho com espaço fica entre aspas
    assert args.endswith(" run") and "--source-root" not in args
    assert "--source-root" in build_run_arguments(cfg, include_source_root=True)


def test_xml_da_tarefa_tem_o_que_prometemos():
    xml = build_task_xml(command=r"C:\Py\pythonw.exe", arguments='-m x --data-dir "C:\\a & b" run',
                         working_dir=r"C:\Users\fulano", user=r"EMPRESA\fulano",
                         interval_minutes=15, start=datetime(2026, 11, 11, 16, 30))
    root = parse(xml)
    # QUANDO: no logon (com 1 min de folga) e a cada 15 minutos, sem prazo para acabar.
    assert text(root, "t:Triggers/t:LogonTrigger/t:UserId") == r"EMPRESA\fulano"
    assert text(root, "t:Triggers/t:LogonTrigger/t:Delay") == "PT1M"
    assert text(root, "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT15M"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Duration", NS) is None
    assert text(root, "t:Triggers/t:TimeTrigger/t:StartBoundary") == "2026-11-11T16:30:00"
    # COM QUAL CONTA: a do usuário, sem elevação, só com ele logado (sem senha guardada).
    assert text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    # COMO: prioridade baixa, uma execução por vez, com limite de tempo, também na bateria.
    assert text(root, "t:Settings/t:Priority") == "7"
    assert text(root, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert text(root, "t:Settings/t:ExecutionTimeLimit") == "PT30M"
    assert text(root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert text(root, "t:Settings/t:StartWhenAvailable") == "true"
    assert text(root, "t:Settings/t:WakeToRun") == "false"
    # O QUE: o comando e os argumentos, com caracteres especiais preservados.
    assert text(root, "t:Actions/t:Exec/t:Command") == r"C:\Py\pythonw.exe"
    assert text(root, "t:Actions/t:Exec/t:Arguments") == '-m x --data-dir "C:\\a & b" run'


def test_xml_sem_gatilho_de_logon():
    xml = build_task_xml(command="p", arguments="a", working_dir="w", user="u",
                         interval_minutes=30, start=datetime(2026, 1, 1), logon_trigger=False)
    root = parse(xml)
    assert root.find("t:Triggers/t:LogonTrigger", NS) is None
    assert text(root, "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT30M"


def test_nome_padrao_inclui_o_usuario(monkeypatch):
    monkeypatch.setenv("USERNAME", "maria da silva")
    assert default_task_name() == "office-chat-keeper-maria_da_silva"


# ------------------------------------------- instalação (schtasks de mentira)
class FakeSchtasks:
    """Anota as chamadas e responde com os códigos de saída combinados."""

    def __init__(self, monkeypatch, fail_create: int = 0, exists: bool = True):
        self.calls: list[tuple[str, ...]] = []
        self.fail_create = fail_create
        self.exists = exists
        self.xml_seen: list[str] = []
        monkeypatch.setattr(scheduler, "_schtasks", self)
        monkeypatch.setattr(scheduler, "current_user", lambda: (r"EMPRESA\fulano", "S-1-5-21-1"))

    def __call__(self, *args: str):
        self.calls.append(args)
        code, out = 0, ""
        if args[0] == "/Create":
            self.xml_seen.append(Path(args[args.index("/XML") + 1]).read_bytes().decode("utf-16"))
            if self.fail_create > 0:
                self.fail_create -= 1
                code, out = 1, "ERRO: Acesso negado."
        elif args[0] == "/Query":
            code = 0 if self.exists else 1
            if "/V" in args and self.exists:
                out = ('"PC","\\\\tarefa","11/11/2026 16:45:00","Pronto","Interativo",'
                       '"11/11/2026 16:30:01","0","autor"\r\n') * 2
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")


def test_install_cria_a_tarefa_e_dispara_a_primeira_execucao(cfg, monkeypatch):
    fake = FakeSchtasks(monkeypatch)
    result = install_task(cfg, interval_minutes=20, task_name="tarefa-teste")

    assert [c[0] for c in fake.calls] == ["/Create", "/Run"]
    create = fake.calls[0]
    assert create[create.index("/TN") + 1] == "tarefa-teste" and "/F" in create
    assert result.logon_trigger and result.started and result.interval_minutes == 20
    assert "<LogonTrigger>" in fake.xml_seen[0] and "PT20M" in fake.xml_seen[0]
    assert Path(result.command).name.lower() in ("pythonw.exe", "python.exe")
    # A definição fica salva (em UTF-16, como o XML declara) para conferência.
    assert result.xml_path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert query_task(cfg).interval_minutes == 20


def test_install_sem_gatilho_de_logon_quando_o_windows_recusa(cfg, monkeypatch):
    fake = FakeSchtasks(monkeypatch, fail_create=1)
    result = install_task(cfg, task_name="tarefa-teste", run_now=False)
    assert [c[0] for c in fake.calls] == ["/Create", "/Create"]
    assert result.logon_trigger is False and result.started is False
    assert "<LogonTrigger>" in fake.xml_seen[0] and "<LogonTrigger>" not in fake.xml_seen[1]


def test_install_recusada_duas_vezes_vira_erro_claro(cfg, monkeypatch):
    FakeSchtasks(monkeypatch, fail_create=2)
    with pytest.raises(SchedulerError, match="Acesso negado"):
        install_task(cfg, task_name="tarefa-teste")


def test_intervalo_fora_da_faixa_e_recusado(cfg, monkeypatch):
    fake = FakeSchtasks(monkeypatch)
    for bad in (0, 4, 1441):
        with pytest.raises(SchedulerError, match="intervalo"):
            install_task(cfg, interval_minutes=bad)
    assert fake.calls == []


def test_uninstall_e_consulta(cfg, monkeypatch):
    fake = FakeSchtasks(monkeypatch)
    info = query_task(cfg, "tarefa-teste")
    assert (info.status, info.last_result, info.result_hint) == ("Pronto", "0", "sucesso")
    assert uninstall_task("tarefa-teste", cfg) is True
    assert [c[0] for c in fake.calls] == ["/Query", "/Query", "/Delete"]

    fake.exists = False
    assert uninstall_task("tarefa-teste", cfg) is False
    assert query_task(cfg, "tarefa-teste") is None


def test_cli_install_e_uninstall(tmp_path, monkeypatch, capsys):
    fake = FakeSchtasks(monkeypatch)
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    data = tmp_path / "dados"

    assert cli.main(["--data-dir", str(data), "install-task", "--interval", "30"]) == 0
    out = capsys.readouterr().out
    assert "Backup automático ligado" in out and "a cada 30 minutos" in out
    assert "sem privilégio de administrador" in out and "uninstall-task" in out
    assert str(data.resolve()) in fake.xml_seen[0]

    assert cli.main(["--data-dir", str(data), "status"]) == 0
    out = capsys.readouterr().out
    assert "Backup automático:  ligado" in out and "a cada 30 min" in out and "(sucesso)" in out

    assert cli.main(["--data-dir", str(data), "uninstall-task"]) == 0
    out = capsys.readouterr().out
    assert "Backup automático desligado" in out and "Seus dados continuam" in out
    assert data.is_dir()  # desinstalar a tarefa não apaga dados

    assert cli.main(["--data-dir", str(data), "install-task", "--interval", "2"]) == 1
    assert "intervalo" in capsys.readouterr().err


def test_cli_install_com_nome_proprio_lembra_o_nome_na_dica(tmp_path, monkeypatch, capsys):
    """Quando --task-name é usado, a dica de desligamento inclui o nome da tarefa.

    Sem isso, quem segue a dica remove a tarefa de nome padrão e a personalizada
    continua rodando.
    """
    FakeSchtasks(monkeypatch)
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    data = tmp_path / "dados"

    assert cli.main(["--data-dir", str(data), "install-task",
                     "--task-name", "minha-tarefa"]) == 0
    out = capsys.readouterr().out
    assert "Para desligar: office-chat-keeper uninstall-task --task-name minha-tarefa" in out

    # Com o nome padrão a dica continua simples (sem --task-name).
    assert cli.main(["--data-dir", str(data), "install-task"]) == 0
    out = capsys.readouterr().out
    assert "Para desligar: office-chat-keeper uninstall-task" in out
    assert "--task-name" not in out.split("Para desligar:")[-1]


# ---------------------------------------------------------------- trava
def test_trava_impede_duas_execucoes_ao_mesmo_tempo(tmp_path):
    with run_lock(tmp_path / "state") as first:
        assert first is True
        with run_lock(tmp_path / "state") as second:
            assert second is False
    with run_lock(tmp_path / "state") as again:  # liberada ao sair
        assert again is True


def test_run_dispensado_quando_ja_ha_outro_em_andamento(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    data, root = tmp_path / "dados", tmp_path / "origem"
    root.mkdir()
    with run_lock(data / "state"):
        assert cli.main(["--data-dir", str(data), "--source-root", str(root), "run"]) == 0
    assert "Outra execução do backup está em andamento" in capsys.readouterr().out


def test_erro_inesperado_no_run_vai_para_o_log_sem_a_mensagem(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))

    def boom(*args, **kwargs):
        raise ValueError("texto sigiloso de uma conversa")

    monkeypatch.setattr(cli, "discover_sources", boom)
    data = tmp_path / "dados"
    assert cli.main(["--data-dir", str(data), "run"]) == 1
    assert "ERRO inesperado (ValueError)" in capsys.readouterr().err
    log_text = (data / "logs" / "office-chat-keeper.log").read_text(encoding="utf-8")
    assert "falha inesperada ValueError" in log_text and "sigiloso" not in log_text


# ------------------------------------------- scripts PowerShell (alternativa)
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.mark.parametrize("name", ["install-task.ps1", "uninstall-task.ps1"])
def test_scripts_powershell_sao_ascii_e_nao_baixam_nada(name):
    # Verificação ESTÁTICA: os testes não executam os scripts.
    raw = (SCRIPTS / name).read_bytes()
    assert max(raw) < 128  # só ASCII: o PowerShell 5.1 lê certo com qualquer codificação
    text_ = raw.decode("ascii").lower()
    for forbidden in ("invoke-expression", "iex ", "invoke-webrequest", "irm ", "downloadstring",
                      "start-bitstransfer", "-encodedcommand", "runlevel highest"):
        assert forbidden not in text_


def test_script_de_instalacao_define_a_mesma_tarefa_que_o_schtasks():
    text_ = (SCRIPTS / "install-task.ps1").read_text(encoding="ascii")
    for expected in ("-AtLogOn", "-RepetitionInterval", "-LogonType Interactive",
                     "-RunLevel Limited", "-MultipleInstances IgnoreNew", "-Priority 7",
                     "-StartWhenAvailable", "pythonw.exe",
                     '-P -m office_chat_keeper --data-dir "{0}" run', "Register-ScheduledTask"):
        assert expected in text_
    assert "Unregister-ScheduledTask" in (SCRIPTS / "uninstall-task.ps1").read_text(encoding="ascii")


# ------------------------------------------------------ teste real, opcional
@pytest.mark.skipif(os.environ.get("OFFICE_CHAT_KEEPER_TEST_SCHEDULER") != "1" or os.name != "nt",
                    reason="cria uma tarefa de verdade; ligue com OFFICE_CHAT_KEEPER_TEST_SCHEDULER=1")
def test_real_cria_consulta_e_remove_uma_tarefa(cfg):
    name = f"office-chat-keeper-pytest-{os.getpid()}"
    try:
        result = install_task(cfg, task_name=name, run_now=False)
        assert query_task(cfg, name) is not None
        assert result.xml_path.is_file()
    finally:
        uninstall_task(name, cfg)
    assert query_task(cfg, name) is None
