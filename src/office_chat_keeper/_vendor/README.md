# Código de terceiros embutido

Esta pasta contém cópias de módulos de dois projetos da CCL Solutions, ambos
sob licença MIT (os arquivos `LICENSE` de cada um estão nas subpastas):

| Projeto | Origem | Versão copiada |
|---|---|---|
| `ccl_chromium_reader` | https://github.com/cclgroupltd/ccl_chromium_reader | tag `v0.3.19`, commit `246ece9f1508ae0a91534b43fe01ddc6299c260c` |
| `ccl_simplesnappy` | https://github.com/cclgroupltd/ccl_simplesnappy | versão 0.4, commit `3d085230baa8c46cf2090ebba29bf6e8eab31087` |

## Por que embutir

Esses projetos não estão no PyPI. Declará-los como dependência obrigaria quem
instala a ter o Git, e traria junto dependências que a leitura do IndexedDB não
usa (`Brotli`, `zstd`). Embutindo só o necessário, o office-chat-keeper não tem
nenhuma dependência externa, e o código que lê os seus dados é uma cópia fixa,
que não muda sem que alguém a troque aqui de propósito.

## O que foi copiado

Só o necessário para ler o IndexedDB: `ccl_chromium_indexeddb.py`,
`structures.py`, `serialization_formats/ccl_blink_value_deserializer.py`,
`serialization_formats/ccl_v8_value_deserializer.py`,
`storage_formats/ccl_leveldb.py` e o pacote `ccl_simplesnappy`.

## O que foi alterado

O mínimo para os módulos funcionarem dentro deste pacote. Cada arquivo alterado
tem uma nota no topo.

- `ccl_chromium_indexeddb.py` e `storage_formats/ccl_leveldb.py`:
  `import ccl_simplesnappy` virou um import relativo.
- `structures.py`: a classe `ArtifactLocation` deixou de herdar de
  `ArtifactLocationProtocol`, que fica em um módulo não embutido.
- Os `__init__.py` de `ccl_chromium_reader` e de seus subpacotes estão vazios
  (o original importa módulos que não foram embutidos).

E uma correção de lógica:

- `ccl_chromium_indexeddb.py`, `IndexedDb.get_blob_info`: o original guardava o
  "endereço do blob" de cada chave na ordem em que os registros aparecem nos
  arquivos, e ficava com o último. Essa ordem não é a ordem de gravação, então
  ele podia apontar para o blob de uma versão antiga, já apagado, e o registro
  ficava ilegível (`FileNotFoundError`). Agora vale a versão de maior número de
  sequência, e uma chave cuja versão mais nova foi apagada fica sem blob. Teste:
  `test_usa_o_blob_da_versao_mais_nova_mesmo_fora_de_ordem`.

Para atualizar, copie de novo os arquivos da versão desejada, reaplique as
alterações acima e rode os testes.
