# Desenvolvimento

## Preparar o ambiente

Requisitos: Windows, Python 3.11 ou superior e Git.

```
git clone https://github.com/gomesmarcusvinicius/office-chat-keeper.git
cd office-chat-keeper
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

`pip install -e` é uma instalação "editável": o comando `office-chat-keeper`
passa a rodar o código da pasta `src\`, então qualquer alteração vale na hora.
A única dependência de desenvolvimento é o `pytest`.

## Rodar os testes

```
.venv\Scripts\python -m pytest
```

- Os testes usam **somente fixtures sintéticas**, criadas por código em
  `tests\idb_builder.py` (um gerador de IndexedDB falso, no mesmo formato de
  disco do Chromium). Nenhum dado real de conversa pode entrar no repositório.
- Nenhum teste cria tarefa agendada, mostra notificação ou toca na pasta do
  Office de verdade: essas partes são trocadas por dublês.
- Há um teste opcional que cria e remove uma tarefa agendada real. Ele só roda
  com a variável `OFFICE_CHAT_KEEPER_TEST_SCHEDULER=1`.
- O script `restore_script.js` roda em navegador e não é executado pelo pytest;
  ali ele é conferido de forma estática. Para ensaiá-lo, gere um pacote com
  `office-chat-keeper restore-script` e siga o `PASSOS.md` (parte 1).

O GitHub Actions roda os mesmos testes em `windows-latest` a cada push e pull
request (arquivo `.github\workflows\tests.yml`).

## Antes de qualquer commit

```
python tools\verificar_antes_de_publicar.py
```

O script procura, nos arquivos que iriam para o repositório, arquivos
proibidos (bancos, snapshots, logs), padrões de segredos e dados da sua máquina
(usuário, domínio, ids e títulos de conversas reais do seu backup local). Sai
com erro se achar algo.

## Estrutura

```
src\office_chat_keeper\
    discovery.py       acha as fontes pivot.claude.ai
    snapshot.py        cópia bruta, retenção
    reader.py          LevelDB -> IndexedDB -> objetos Python
    conversation.py    registro bruto -> conversa
    archive.py         arquivo permanente (SQLite) e busca (FTS5)
    export.py          Markdown, JSON, imagens
    pipeline.py        encadeia snapshot -> arquivo -> exportação
    verify.py          verificação periódica, relatórios, plano de restauração
    notify.py          notificação do Windows
    scheduler.py       tarefa agendada (schtasks)
    restore.py         estratégia A (único módulo que escreve na pasta do Office)
    restore_script.py  estratégia B: gera o pacote
    restore_script.js  estratégia B: script para o DevTools
    protect.py         permissão da pasta de dados (icacls)
    lock.py            trava de execução
    cli.py             linha de comando
    _vendor\           leitor de IndexedDB da CCL Solutions (MIT), embutido
scripts\               alternativa em PowerShell para o agendamento
tools\                 verificação antes de publicar
tests\                 testes com fixtures sintéticas
```

## Código embutido de terceiros

A pasta `src\office_chat_keeper\_vendor` contém cópias de módulos do
`ccl_chromium_reader` e do `ccl_simplesnappy` (CCL Solutions, MIT). O arquivo
`_vendor\README.md` diz a versão copiada, o que foi alterado e como atualizar.

## Gerar o pacote

Uma **wheel** (`.whl`) é o pacote pronto para instalar: um zip com o código já
organizado.

```
python -m pip wheel . --no-deps -w dist
```

Para conferir, instale-a em um ambiente virtual novo:

```
python -m venv %TEMP%\ock-teste
%TEMP%\ock-teste\Scripts\python -m pip install --no-index dist\office_chat_keeper-0.1.1-py3-none-any.whl
%TEMP%\ock-teste\Scripts\office-chat-keeper --version
```

## Publicar uma versão

1. Atualize `__version__` em `src\office_chat_keeper\__init__.py` e o
   `CHANGELOG.md`.
2. Rode os testes e a verificação antes de publicar.
3. Crie a **tag** (um nome fixo para um ponto do histórico): `git tag v0.1.1`
   e `git push origin v0.1.1`. Como a `main` exige pull request, a troca de
   versão entra por uma branch e um PR; a tag é criada depois, no commit da
   `main` que resultou do merge.
4. Gere a wheel e crie a **Release** no GitHub a partir da tag, anexando a
   wheel. A Release é a página de download de uma versão.

As instruções de instalação públicas devem sempre apontar para uma tag, nunca
para a branch `main`.

## Scripts PowerShell

`scripts\install-task.ps1` e `scripts\uninstall-task.ps1` criam e removem a
mesma tarefa agendada usando os cmdlets do PowerShell. São uma alternativa ao
comando `install-task`, que usa o `schtasks.exe` e funciona mesmo quando a
política de execução do PowerShell bloqueia scripts não assinados. Esses
scripts foram conferidos apenas por análise estática.
