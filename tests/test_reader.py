"""Testes do parser: LevelDB -> IndexedDB -> objeto V8 -> Python.

Usam um banco sintético gerado por ``idb_builder`` (nenhum dado real).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from office_chat_keeper.reader import (UNDEFINED, SensitiveDatabaseError, SnapshotReader,
                                       normalize)

from idb_builder import FakeIndexedDB, build_sample_storage, sample_chat

CHAT_ID = "11111111-2222-3333-4444-555555555555"


def open_reader(db: FakeIndexedDB) -> SnapshotReader:
    db.write()
    return SnapshotReader(db.leveldb_dir, db.blob_dir)


def test_le_o_esquema(tmp_path):
    with open_reader(build_sample_storage(tmp_path)) as reader:
        schema = reader.schema()
    assert schema["claude-chat-history"] == ["chats", "blobs"]
    assert set(schema) == {"claude-chat-history", "claude-mcp-gateways",
                           "claude-local-skills", "claude-memory"}


@pytest.mark.parametrize("external", [True, False], ids=["valor-na-pasta-blob", "valor-inline"])
def test_le_a_conversa_com_tipos_corretos(tmp_path, external):
    with open_reader(build_sample_storage(tmp_path, external=external)) as reader:
        records = list(reader.records("claude-chat-history", "chats"))
        assert reader.errors == []

    assert len(records) == 1
    record = records[0]
    assert record.key == CHAT_ID
    assert record.external is external
    assert not record.deleted and not record.superseded

    value = record.value
    assert value["title"] == "Conciliação de teste"  # acento: str, não bytes
    assert value["hostOrigin"] is None
    messages = value["snapshot"]["ui"]["messages"]
    assert messages[0]["files"] is UNDEFINED  # undefined do JS é preservado
    assert messages[0]["timestamp"] == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert messages[-1]["content"].endswith("🎉")  # texto de dois bytes (UTF-16)
    assert messages[2]["result"] == {"success": True, "result": {"rows": [["a", "b"]]}}
    assert messages[2]["resultChars"] == 40


def test_dado_binario_continua_bytes(tmp_path):
    with open_reader(build_sample_storage(tmp_path)) as reader:
        blobs = list(reader.records("claude-chat-history", "blobs"))
    assert blobs[0].key == [CHAT_ID, "dados"]  # chave composta vira lista
    assert blobs[0].value["bytes"] == b'{"linhas": 3}'


def test_so_a_versao_mais_nova_de_cada_chave(tmp_path):
    db = build_sample_storage(tmp_path, chats=[sample_chat(title="Versão antiga")])
    db.put("claude-chat-history", "chats", CHAT_ID, sample_chat(title="Versão nova"))
    with open_reader(db) as reader:
        latest = list(reader.records("claude-chat-history", "chats"))
        everything = list(reader.records("claude-chat-history", "chats", include_superseded=True))

    assert [r.value["title"] for r in latest] == ["Versão nova"]
    assert [(r.value["title"], r.superseded) for r in everything] == [
        ("Versão nova", False), ("Versão antiga", True)]
    assert everything[0].seq > everything[1].seq


def test_chave_apagada_aparece_como_removida(tmp_path):
    db = build_sample_storage(tmp_path)
    db.delete("claude-chat-history", "chats", CHAT_ID)
    with open_reader(db) as reader:
        records = list(reader.records("claude-chat-history", "chats"))
        older = list(reader.records("claude-chat-history", "chats", include_superseded=True))
    assert len(records) == 1 and records[0].deleted and records[0].value is None
    # A versão anterior ainda está no arquivo e pode ser recuperada.
    assert older[1].value["id"] == CHAT_ID and older[1].superseded


def test_valor_maior_que_um_bloco_do_leveldb(tmp_path):
    big = sample_chat()
    big["snapshot"]["ui"]["messages"][0]["content"] = "ç" * 100_000  # > 32 KB
    with open_reader(build_sample_storage(tmp_path, chats=[big], external=False)) as reader:
        record = next(reader.records("claude-chat-history", "chats"))
    assert record.value["snapshot"]["ui"]["messages"][0]["content"] == "ç" * 100_000


def test_recusa_ler_o_banco_de_tokens(tmp_path):
    with open_reader(build_sample_storage(tmp_path)) as reader:
        with pytest.raises(SensitiveDatabaseError):
            list(reader.records("claude-mcp-gateways", "gateways"))


def test_banco_ou_store_inexistente_devolve_vazio(tmp_path):
    with open_reader(build_sample_storage(tmp_path)) as reader:
        assert list(reader.records("nao-existe", "chats")) == []
        assert list(reader.records("claude-chat-history", "nao-existe")) == []


def test_leitura_nao_altera_os_arquivos(tmp_path):
    db = build_sample_storage(tmp_path)
    db.write()

    def state():
        return {str(p.relative_to(tmp_path)): (p.stat().st_mtime_ns,
                                                hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    before = state()
    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        list(reader.records("claude-chat-history", "chats"))
        list(reader.records("claude-chat-history", "blobs"))
    assert state() == before


def test_blob_ausente_vira_erro_registrado_sem_derrubar(tmp_path):
    db = build_sample_storage(tmp_path)
    db.write()
    for blob in list(db.blob_dir.rglob("*")):
        if blob.is_file():
            blob.unlink()
    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        assert list(reader.records("claude-chat-history", "chats")) == []
        assert len(reader.errors) == 1 and CHAT_ID in reader.errors[0][2]
        # Os outros registros continuam legíveis.
        assert len(list(reader.records("claude-chat-history", "blobs"))) == 1


def test_usa_o_blob_da_versao_mais_nova_mesmo_fora_de_ordem(tmp_path):
    """Regressão: com várias versões do "endereço do blob", vale a mais nova.

    No banco real a ordem dos registros nos arquivos (.log e tabelas .ldb) não
    segue a ordem de gravação. O leitor chegou a usar o endereço de uma versão
    antiga, cujo blob o Chromium já tinha apagado, e a conversa ficava ilegível.
    """
    db = build_sample_storage(tmp_path, chats=[sample_chat(title="Versão antiga")])
    old_blobs = [p for p in db.blob_dir.rglob("*") if p.is_file()]
    db.put("claude-chat-history", "chats", CHAT_ID, sample_chat(title="Versão nova"),
           external=True)
    for blob in old_blobs:
        blob.unlink()  # o Chromium apaga o blob da versão substituída
    db.write()

    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        reader._raw._fetched_records.reverse()  # versão antiga por último no arquivo
        records = list(reader.records("claude-chat-history", "chats"))
        assert reader.errors == []
    assert [r.value["title"] for r in records] == ["Versão nova"]


def test_normalize_marca_datas_como_utc():
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert normalize({"d": naive, "l": (1, 2)}) == {
        "d": naive.replace(tzinfo=timezone.utc), "l": [1, 2]}
