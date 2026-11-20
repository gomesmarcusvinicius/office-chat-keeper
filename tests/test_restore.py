"""Testes da estratégia A (restaurar snapshot bruto).

TODOS rodam em pastas temporárias: a "pasta do Office" aqui é um diretório de
teste com um banco sintético. Nenhum teste aponta para o armazenamento real.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from office_chat_keeper import cli, restore
from office_chat_keeper.archive import Archive
from office_chat_keeper.discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME
from office_chat_keeper.reader import SnapshotReader
from office_chat_keeper.restore import storage_is_free

from idb_builder import CHAT_HISTORY_SCHEMA, build_sample_storage, sample_chat

CHAT_A = "11111111-2222-3333-4444-555555555555"
CHAT_B = "99999999-8888-7777-6666-555555555555"
IDB = Path("EBWebView") / "Default" / "IndexedDB"


def tree(root: Path) -> dict[str, str]:
    """Nome -> hash de todos os arquivos (para provar que nada mudou)."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def chat_ids(folder: Path) -> set[str]:
    blob = folder / BLOB_DIR_NAME
    with SnapshotReader(folder / LEVELDB_DIR_NAME, blob if blob.is_dir() else None) as reader:
        return set(reader.keys("claude-chat-history", "chats"))


class Env:
    def __init__(self, tmp_path: Path):
        self.data = tmp_path / "dados"
        self.root = tmp_path / "office"
        self.folder = self.root / "a_ADAL" / "2" / "0" / IDB
        self.confirmations: list[str] = []
        self.answer = True

    def cli(self, *command: str) -> int:
        return cli.main(["--data-dir", str(self.data), "--source-root", str(self.root), *command])

    def rewrite(self, chats: list[dict], **kwargs) -> None:
        """Simula o Office recriando o banco com outro conteúdo."""
        if self.folder.exists():
            shutil.rmtree(self.folder)
        build_sample_storage(self.folder, chats=chats, **kwargs).write()

    @property
    def source_id(self) -> str:
        (only,) = [p for p in (self.data / "snapshots").iterdir() if p.is_dir()]
        return only.name

    def snapshots(self) -> list[str]:
        return sorted(p.name for p in (self.data / "snapshots" / self.source_id).iterdir())

    def spec(self, index: int) -> str:
        return f"{self.source_id}/{self.snapshots()[index]}"


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    """Fonte que TINHA a conversa A e agora só tem a B (dois snapshots tirados)."""
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    monkeypatch.setattr(restore, "running_office_apps", lambda: [])
    e = Env(tmp_path)
    monkeypatch.setattr(cli, "_ask_confirmation",
                        lambda phrase: e.confirmations.append(phrase) or e.answer)
    e.rewrite([sample_chat()])
    assert e.cli("run") == 0
    e.rewrite([sample_chat(CHAT_B, title="Conversa nova")])
    assert e.cli("run") == 0
    return e


# ------------------------------------------------------------ simulação
def test_lista_os_snapshots_restauraveis(env, capsys):
    capsys.readouterr()
    assert env.cli("restore-snapshot") == 0
    out = capsys.readouterr().out
    assert env.spec(0) in out and env.spec(1) in out
    assert "1 conversa(s)" in out and "última cópia bruta de alguma conversa" in out
    assert "A simulação não altera nada" in out


def test_simulacao_mostra_o_que_mudaria_e_nao_altera_nada(env, capsys):
    capsys.readouterr()
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0)) == 0
    out = capsys.readouterr().out

    assert "PLANO DE RESTAURAÇÃO" in out and "Nenhum impedimento encontrado." in out
    assert "VOLTAM ao Office (1):" in out and "11111111  Conciliação de teste" in out
    assert "SAEM do Office, mas continuam no arquivo permanente (1):" in out
    assert "99999999  Conversa nova" in out
    assert "SIMULAÇÃO: nada foi alterado" in out
    assert tree(env.root) == before
    assert not (env.data / "pre-restore").exists() and env.confirmations == []


# --------------------------------------------------------------- execução
def test_restauracao_completa_com_copia_de_seguranca_e_conferencia(env, capsys):
    capsys.readouterr()
    assert chat_ids(env.folder) == {CHAT_B}

    def recreated_alerts() -> int:
        with Archive(env.data / "archive" / "conversas.sqlite") as arch:
            return sum(r["kind"] == "database_recreated" for r in arch.all_alerts())

    recreated_before = recreated_alerts()
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 0
    out = capsys.readouterr().out

    assert env.confirmations == [f"RESTAURAR {env.source_id}"]
    assert "Restauração concluída e conferida: 1 conversa(s)" in out
    assert chat_ids(env.folder) == {CHAT_A}  # o banco voltou a ter a conversa A
    # Nenhuma pasta provisória ou "antiga" ficou para trás na pasta do Office.
    assert sorted(p.name for p in env.folder.iterdir() if "pivot.claude.ai" in p.name) == [
        BLOB_DIR_NAME, LEVELDB_DIR_NAME]
    # A cópia de segurança guarda o estado anterior (que tinha a conversa B).
    (backup,) = (env.data / "pre-restore").iterdir()
    assert chat_ids(backup) == {CHAT_B} and (backup / "manifest.json").is_file()
    report = next((env.data / "reports").glob("restauracao-*.md")).read_text(encoding="utf-8")
    assert "Para desfazer" in report and str(backup) in report

    # O backup continua funcionando depois: A reaparece, B passa a "ausente", nada se perde.
    assert env.cli("run") == 0
    with Archive(env.data / "archive" / "conversas.sqlite") as arch:
        assert arch.load_conversation(CHAT_A).missing_since is None
        assert arch.load_conversation(CHAT_B).missing_since is not None
        assert len(arch.load_conversation(CHAT_B).messages) == 5
    # O banco "recriado" pela nossa própria restauração não vira alerta de perda.
    assert recreated_alerts() == recreated_before
    assert not (env.data / "state" / "restored.json").read_text(encoding="utf-8").strip("{} \n")


def test_desfazer_usando_a_copia_de_seguranca(env, capsys):
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 0
    assert env.cli("run") == 0
    (backup,) = (env.data / "pre-restore").iterdir()
    capsys.readouterr()

    assert env.cli("restore-snapshot", str(backup), "--execute") == 0
    assert chat_ids(env.folder) == {CHAT_B}
    assert len(list((env.data / "pre-restore").iterdir())) == 2  # cada troca tem a sua cópia


# ------------------------------------------------------------------ travas
def assert_blocked(env: Env, capsys, expected: str, *extra: str) -> None:
    capsys.readouterr()
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0), "--execute", *extra) == 1
    out = capsys.readouterr().out
    assert "NÃO PODE SER EXECUTADO AGORA" in out and expected in out
    assert "Nada foi alterado." in out
    assert tree(env.root) == before
    assert env.confirmations == []  # nem chega a pedir confirmação
    assert not (env.data / "pre-restore").exists()


def test_bloqueia_com_app_do_office_aberto(env, capsys, monkeypatch):
    monkeypatch.setattr(restore, "running_office_apps", lambda: ["EXCEL.EXE", "OUTLOOK.EXE"])
    assert_blocked(env, capsys, "feche antes: EXCEL.EXE, OUTLOOK.EXE")


def test_bloqueia_com_o_banco_em_uso(env, capsys):
    lock = env.folder / LEVELDB_DIR_NAME / "LOCK"
    assert storage_is_free(lock.parent) is True
    with open(lock, "rb"):  # outro "processo" segurando o arquivo
        assert storage_is_free(lock.parent) is False
        assert_blocked(env, capsys, "o banco da fonte está em uso por algum processo")
    assert storage_is_free(lock.parent) is True


def test_bloqueia_se_o_estado_atual_nao_esta_no_backup(env, capsys):
    with open(env.folder / LEVELDB_DIR_NAME / "000003.log", "ab") as fh:
        fh.write(b"\x00" * 16)  # a fonte mudou depois do último 'run'
    assert_blocked(env, capsys, "rode 'office-chat-keeper run' antes")


def test_bloqueia_snapshot_adulterado(env, capsys):
    snapshot = env.data / "snapshots" / env.source_id / env.snapshots()[0]
    with open(snapshot / LEVELDB_DIR_NAME / "000003.log", "ab") as fh:
        fh.write(b"lixo")
    assert_blocked(env, capsys, "o snapshot não está íntegro")


def test_bloqueia_se_o_esquema_mudou(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    monkeypatch.setattr(restore, "running_office_apps", lambda: [])
    e = Env(tmp_path)
    monkeypatch.setattr(cli, "_ask_confirmation", lambda phrase: e.confirmations.append(phrase) or True)
    e.rewrite([sample_chat()], version=10)
    assert e.cli("run") == 0
    e.rewrite([sample_chat(CHAT_B)], version=20)  # o suplemento atualizou o banco
    assert e.cli("run") == 0
    assert_blocked(e, capsys, "versão 10 no snapshot, 20 na fonte")


def _env_com_banco_extra(tmp_path, monkeypatch, *, extra_no_snapshot: bool) -> Env:
    """Fonte reescrita uma vez; o banco 'claude-office-snipped-results' existe só
    no snapshot antigo (o suplemento cria esse banco sob demanda) ou só na fonte."""
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    monkeypatch.setattr(restore, "running_office_apps", lambda: [])
    e = Env(tmp_path)
    monkeypatch.setattr(cli, "_ask_confirmation", lambda phrase: e.confirmations.append(phrase) or True)
    for chats, with_extra in (([sample_chat()], extra_no_snapshot),
                              ([sample_chat(CHAT_B)], not extra_no_snapshot)):
        if e.folder.exists():
            shutil.rmtree(e.folder)
        db = build_sample_storage(e.folder, chats=chats)
        if with_extra:
            db.add_database("claude-office-snipped-results", ["results"])
        db.write()
        assert e.cli("run") == 0
    return e


def test_banco_a_mais_no_snapshot_so_avisa(tmp_path, monkeypatch, capsys):
    e = _env_com_banco_extra(tmp_path, monkeypatch, extra_no_snapshot=True)
    capsys.readouterr()
    assert e.cli("restore-snapshot", e.spec(0)) == 0
    out = capsys.readouterr().out
    assert "Nenhum impedimento encontrado." in out
    assert "AVISO: o banco 'claude-office-snipped-results' existe no snapshot" in out


def test_banco_a_mais_na_fonte_continua_bloqueando(tmp_path, monkeypatch, capsys):
    e = _env_com_banco_extra(tmp_path, monkeypatch, extra_no_snapshot=False)
    assert_blocked(e, capsys, "o banco 'claude-office-snipped-results' existe na fonte e não no snapshot")


def test_confirmacao_errada_cancela(env, capsys):
    capsys.readouterr()
    env.answer = False
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 1
    assert "Cancelado. Nada foi alterado." in capsys.readouterr().out
    assert tree(env.root) == before and not (env.data / "pre-restore").exists()


def test_sem_terminal_interativo_nao_executa(env, capsys, monkeypatch):
    # A confirmação de verdade: sob pytest (ou agendado) não há terminal, então recusa.
    monkeypatch.undo()
    monkeypatch.setattr(restore, "running_office_apps", lambda: [])
    capsys.readouterr()
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 1
    out = capsys.readouterr().out
    assert "exige um terminal interativo" in out and "Cancelado" in out
    assert tree(env.root) == before


# -------------------------------------------------------- desfazer sozinho
def test_se_a_conferencia_falha_a_troca_e_desfeita(env, capsys, monkeypatch):
    real = restore._read_chat_ids
    live = env.folder / LEVELDB_DIR_NAME

    def lying(leveldb_dir, blob_dir):
        result = real(leveldb_dir, blob_dir)
        return {"conversa-errada"} if Path(leveldb_dir) == live else result

    monkeypatch.setattr(restore, "_read_chat_ids", lying)
    capsys.readouterr()
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 1
    err = capsys.readouterr().err
    assert "A troca foi DESFEITA; a fonte está como antes." in err
    assert tree(env.root) == before  # exatamente os mesmos arquivos de antes
    assert len(list((env.data / "pre-restore").iterdir())) == 1  # a cópia de segurança fica


def test_se_a_troca_falha_no_meio_tudo_volta(env, capsys, monkeypatch):
    real_rename = restore.os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:  # 1 e 2: pastas atuais saem; 3: a nova não consegue entrar
            raise PermissionError("arquivo em uso")
        return real_rename(src, dst)

    monkeypatch.setattr(restore.os, "rename", flaky)
    capsys.readouterr()
    before = tree(env.root)
    assert env.cli("restore-snapshot", env.spec(0), "--execute") == 1
    assert "DESFEITA" in capsys.readouterr().err
    monkeypatch.setattr(restore.os, "rename", real_rename)
    assert tree(env.root) == before
    assert chat_ids(env.folder) == {CHAT_B}


def test_duas_restauracoes_no_mesmo_segundo_nao_colidem(env, monkeypatch):
    # Em uma máquina rápida, restaurar e desfazer podem cair no mesmo segundo:
    # cada troca precisa da sua própria pasta de cópia de segurança.
    from datetime import datetime, timezone

    from office_chat_keeper.config import load_config
    from office_chat_keeper.discovery import discover_sources

    cfg = load_config(env.data, env.root)
    now = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)

    def restore_once(spec: str) -> Path:
        (target,) = discover_sources(env.root)
        with Archive(cfg.archive_path) as archive:
            plan = restore.build_plan(cfg, archive, restore.resolve_restore_source(cfg, spec), target)
        assert plan.blockers == []
        return restore.execute_restore(cfg, plan, now=now).backup_dir

    first = restore_once(env.spec(0))
    assert env.cli("run") == 0
    second = restore_once(str(first))
    assert second.name == first.name + "-2"
    assert chat_ids(env.folder) == {CHAT_B}


def test_unique_dir(tmp_path):
    base = tmp_path / "pasta"
    assert restore.unique_dir(base) == base
    base.mkdir()
    assert restore.unique_dir(base) == tmp_path / "pasta-2"
    (tmp_path / "pasta-2").mkdir()
    assert restore.unique_dir(base) == tmp_path / "pasta-3"


# ----------------------------------------------------------- outra pasta
def test_restaurar_em_outra_pasta_exige_indicar_o_destino(env, capsys):
    # O Office passou a usar 2\1: a pasta original do snapshot não existe mais.
    spec = env.spec(0)
    new_folder = env.root / "a_ADAL" / "2" / "1" / IDB
    shutil.rmtree(env.root / "a_ADAL" / "2" / "0")
    build_sample_storage(new_folder, chats=[]).write()
    assert env.cli("run") == 0
    capsys.readouterr()

    assert env.cli("restore-snapshot", spec) == 2
    err = capsys.readouterr().err
    assert "não existe no disco agora" in err and "--target" in err

    target = next(p.name for p in (env.data / "snapshots").iterdir() if p.name not in spec)
    assert env.cli("restore-snapshot", spec, "--target", target) == 0
    out = capsys.readouterr().out
    assert "o snapshot veio de OUTRA pasta" in out and "VOLTAM ao Office (1):" in out

    assert env.cli("restore-snapshot", spec, "--target", target, "--execute") == 0
    assert chat_ids(new_folder) == {CHAT_A}


def test_snapshot_inexistente(env, capsys):
    assert env.cli("restore-snapshot", "nao/existe") == 2
    assert "Não achei um snapshot restaurável" in capsys.readouterr().err


# ---------------------------------------------------- leitor: chaves e esquema
def test_leitor_devolve_chaves_e_detalhes_do_esquema(tmp_path):
    db = build_sample_storage(tmp_path, chats=[sample_chat(), sample_chat(CHAT_B)], version=30)
    db.write()
    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        assert set(reader.keys("claude-chat-history", "chats")) == {CHAT_A, CHAT_B}
        details = reader.schema_details()["claude-chat-history"]
    assert details["version"] == 30
    chats = details["stores"]["chats"]
    assert chats["key_path"] == "id" and chats["auto_increment"] is False
    assert {name: spec["key_path"] for name, spec in chats["indexes"].items()} == \
        CHAT_HISTORY_SCHEMA["chats"]["indexes"]
    assert details["stores"]["blobs"]["key_path"] == ["chatId", "name"]


def test_chave_apagada_nao_aparece_em_keys(tmp_path):
    db = build_sample_storage(tmp_path)
    db.delete("claude-chat-history", "chats", CHAT_A)
    db.write()
    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        assert reader.keys("claude-chat-history", "chats") == []
