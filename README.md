# office-chat-keeper

Backup local, contínuo e legível do histórico de conversas dos suplementos do
Claude no Office (Excel, Word, PowerPoint e Outlook), para Windows.

> [!WARNING]
> **Projeto experimental, feito por _vibe coding_.** Esta ferramenta foi
> desenvolvida com ajuda de IA (Claude Code) e guiada por um analista de dados,
> não por um desenvolvedor de software profissional. O código pode ter falhas e
> **não passou por revisão profissional**.
>
> É um projeto **não oficial**, sem vínculo com a Anthropic nem com a Microsoft.
> Funciona lendo um armazenamento interno do Office, que pode mudar a qualquer
> atualização. O uso é por sua conta e risco; a licença oferece o software
> "como está", sem garantia.
>
> **Revisões, sugestões e correções são bem-vindas**, principalmente nas partes
> de segurança, leitura do IndexedDB/LevelDB e restauração.

> [!WARNING]
> **Experimental project, built by _vibe coding_.** This tool was written with
> AI assistance (Claude Code) and steered by a data analyst, not a professional
> software developer. The code may contain bugs and **has not been
> professionally reviewed**.
>
> It is **unofficial** and not affiliated with Anthropic or Microsoft. It reads
> an internal Office storage that may change with any update. Use at your own
> risk; the license provides the software "as is", without warranty.
>
> **Code reviews, suggestions and fixes are welcome**, especially around
> security, IndexedDB/LevelDB reading and restoration.

## In English (short version)

The Claude add-ins for Microsoft 365 keep chat history **only on your
computer**, inside the WebView2 storage that Office provides. After some Office
updates the add-in may open in a new, empty storage folder, and the history is
gone. `office-chat-keeper` is a local Windows tool that keeps a continuous,
readable backup of that history so it is not lost again.

- Copies the raw storage read-only, keeps an append-only SQLite archive,
  exports each conversation to Markdown/JSON, offers full-text search and can
  run on a schedule (per-user Windows task, no administrator rights).
- 100% local: no network access, no telemetry.
- It does **not** recover history lost before it was installed.
- Windows 10/11, Python 3.11+. Documentation and messages are in Portuguese.
- Contributions and code reviews are welcome, in English or Portuguese: see
  [CONTRIBUTING.md](CONTRIBUTING.md). Never post real conversation data.

Install (no Git required):

```
py -m pip install --user https://github.com/gomesmarcusvinicius/office-chat-keeper/releases/download/v0.1.0/office_chat_keeper-0.1.0-py3-none-any.whl
```

## O problema

Os suplementos do Claude guardam o histórico **só no seu computador**, no
armazenamento do WebView2 que o Office fornece. Não existe cópia em servidor e
nada sincroniza. Depois de uma atualização, o Office pode abrir o suplemento em
uma pasta de armazenamento nova e vazia, e o histórico antigo some.

## O que a ferramenta faz

- **Copia** o armazenamento bruto com segurança (somente leitura, mesmo com o
  Office aberto).
- **Guarda** as conversas em um arquivo permanente que só cresce: nada do que
  já foi visto sai de lá, mesmo que suma da origem.
- **Avisa** quando há sinal de perda de histórico (fonte nova e vazia, banco
  recriado, conversas que sumiram).
- **Exporta** cada conversa em Markdown e JSON, com as imagens como arquivos.
- **Busca** por termo no texto das mensagens.
- **Roda sozinha** pelo Agendador de Tarefas do Windows, sem janela.

## O que a ferramenta NÃO faz

- **Não recupera o que se perdeu antes da instalação.** O backup precisa existir
  antes da perda.
- **Não é um produto oficial** e não tem suporte da Anthropic nem da Microsoft.
- **A restauração dentro do suplemento não é suportada oficialmente** e pode
  quebrar com atualizações. Ela existe, é manual e foi usada de verdade uma
  única vez, com sucesso (veja [docs/restauracao.md](docs/restauracao.md)). O
  caminho seguro é anexar o Markdown exportado a uma conversa nova.
- **Não funciona no macOS** nem no Office na web.

## Privacidade

- **100% local.** Sem rede, sem telemetria, sem enviar nada a lugar nenhum.
- **Somente leitura na origem.** O backup nunca escreve, move ou apaga nada na
  pasta do Office. A única exceção é o comando de restauração, manual e com
  confirmação digitada.
- **Os snapshots brutos contêm tokens de conectores.** Ficam em uma pasta com
  permissão restrita ao seu usuário.
- **As exportações em Markdown contêm o conteúdo das conversas** e dos
  documentos citados nelas (planilhas, e-mails). Avalie a política de dados da
  sua empresa antes de guardar a pasta de dados em um local sincronizado, como
  o OneDrive corporativo. A ferramenta avisa quando detecta isso.
- **Logs sem conteúdo.** Só contagens, ids e nomes de pasta.

Por padrão tudo fica em `%USERPROFILE%\ClaudeOfficeBackup`.

## Requisitos

- Windows 10 ou 11.
- Python 3.11 ou superior.
- Office com os suplementos do Claude.
- Nenhuma outra dependência: a ferramenta usa só a biblioteca padrão do Python.
- O agendamento é por usuário e **não exige administrador**.

## Instalação

Abra o Prompt de Comando (menu Iniciar, digite `cmd`) e use uma das formas
abaixo. Todas instalam a versão `v0.1.0`.

**1. A mais simples (não precisa de Git):**

```
py -m pip install --user https://github.com/gomesmarcusvinicius/office-chat-keeper/releases/download/v0.1.0/office_chat_keeper-0.1.0-py3-none-any.whl
```

**2. Recomendada, em ambiente isolado (pipx):**

```
py -m pip install --user pipx
py -m pipx ensurepath
```

Feche e reabra o Prompt de Comando e então:

```
pipx install https://github.com/gomesmarcusvinicius/office-chat-keeper/releases/download/v0.1.0/office_chat_keeper-0.1.0-py3-none-any.whl
```

**3. Direto do código (exige Git):**

```
py -m pip install --user git+https://github.com/gomesmarcusvinicius/office-chat-keeper.git@v0.1.0
```

Observações:

- **Se `py` não for reconhecido**, troque `py` por `python` nos comandos. Isso
  acontece com o Python instalado pela Microsoft Store.
- **Se `office-chat-keeper` não for reconhecido** depois de instalar com
  `--user`, a pasta de scripts do Python não está no PATH. Use
  `py -m office_chat_keeper` no lugar de `office-chat-keeper` (funciona sempre),
  ou instale com o pipx, que ajusta o PATH.
- **Se a instalação falhar com `No such file or directory` e uma dica sobre
  "Long Path"**, o caminho de instalação passou do limite de 260 caracteres do
  Windows. Pode acontecer com `--user` no Python da Microsoft Store quando o
  nome de usuário do Windows é longo (cerca de 25 caracteres ou mais). Use a
  forma 2 (pipx), que instala em uma pasta curta.
- **Se o `pip` estiver bloqueado** pela política da sua empresa, a instalação
  falha com erro de acesso ou de proxy. Nesse caso, peça a liberação à TI.

## Primeiros passos

```
office-chat-keeper status
```

Verifica se o armazenamento do Claude no Office foi encontrado.

```
office-chat-keeper run
```

Faz o primeiro backup.

```
office-chat-keeper install-task
```

Liga o backup automático (no logon e a cada 15 minutos).

Para remover:

```
office-chat-keeper uninstall-task
py -m pip uninstall office-chat-keeper
```

Desinstalar não apaga a pasta de dados; apague-a você mesmo se quiser.

## Comandos

| Comando | O que faz |
|---|---|
| `status` | Mostra fontes, snapshots, arquivo, backup automático e alertas. Não altera nada. |
| `run` | Snapshot do que mudou, ingestão no arquivo, exportação e verificação. |
| `list` | Lista as conversas guardadas (`--app excel`, `--limit 10`). |
| `search "termo"` | Busca no texto das mensagens (`--phrase`, `--kind`, `--conversation`, `--limit`). |
| `export` | Refaz a exportação de todas as conversas (`--include-thinking`). |
| `alerts` | Mostra os alertas (`--ack 3`, `--ack all`, `--all`). |
| `verify` | Compara fontes e arquivo e grava um relatório. Não altera nada. |
| `install-task` | Liga o backup automático (`--interval 30`). |
| `uninstall-task` | Desliga o backup automático. Não apaga dados. |
| `restore-snapshot` | Restauração do banco inteiro. Simula por padrão; `--execute` exige confirmação. |
| `restore-script` | Gera o pacote da restauração seletiva. Não grava no Office. |

Opções globais, antes do comando: `--data-dir` (pasta de dados) e
`--source-root` (pasta de origem). Também valem as variáveis de ambiente
`OFFICE_CHAT_KEEPER_HOME` e `OFFICE_CHAT_KEEPER_SOURCE_ROOT`.

A busca ignora maiúsculas e acentos. Várias palavras precisam estar na mesma
mensagem; `--phrase` busca a frase exata e `*` no fim busca por prefixo:

```
office-chat-keeper search "fluxo de caixa"
office-chat-keeper search --phrase "orçamento de março"
office-chat-keeper search --kind user --kind assistant concilia*
```

## Onde os dados ficam

```
%USERPROFILE%\ClaudeOfficeBackup\
    snapshots\      cópias brutas do armazenamento (contêm tokens de conectores)
    archive\        arquivo permanente em SQLite (só cresce)
    exports\        Markdown, JSON e imagens de cada conversa
    reports\        relatórios de verificação e planos de restauração (simulações)
    pre-restore\    cópias de segurança feitas antes de cada restauração
    restore\        pacotes da restauração seletiva
    logs\           log sem conteúdo de conversa
    state\          estado interno (proteção da pasta, tarefa agendada)
```

## Limitações conhecidas

**O que foi testado**

- Um único armazenamento real, de uma única conta, no Windows 11, com o Excel e
  o PowerPoint.
- Backup, arquivo permanente, exportação, busca e agendamento, em uso real
  contínuo por cerca de dez dias, incluindo uma perda real de histórico: o
  Office apagou o armazenamento e o recriou vazio, e a ferramenta avisou.
- Restauração do banco inteiro (estratégia A) em uso real, uma vez: as duas
  conversas voltaram e o suplemento as exibiu normalmente.
- Restauração seletiva (estratégia B) apenas em páginas de teste (inclusive
  com os bytes de uma conversa real de cerca de 50 MB).

**O que não foi testado**

- macOS (não suportado).
- Word e Outlook: os nomes internos desses apps são suposições.
- Outras versões do Office e do suplemento.
- Vários usuários ou várias contas na mesma máquina.
- **Restauração seletiva (estratégia B) em uso real.**
- Exportação de skills e de memória com dados reais (só com dados sintéticos).

**O que é frágil**

- O formato do armazenamento é interno e não documentado. Uma atualização do
  suplemento ou do Office pode mudar o esquema e quebrar a leitura.
- A detecção de "banco recriado" depende da data de criação de um arquivo, que
  o Windows às vezes preserva. A detecção de conversas ausentes não depende
  disso.
- O filtro de credenciais das exportações é uma rede de segurança, não uma
  garantia. Revise antes de compartilhar uma exportação.
- O script JavaScript da restauração seletiva não roda nos testes
  automatizados; ali ele é conferido só de forma estática.

## Como foi feito

O código foi gerado com o Claude Code e revisado **apenas pelo autor**, que é
analista de dados. Ninguém deve presumir que ele foi auditado. Os testes
automatizados usam somente dados sintéticos, criados por código.

## Como contribuir

O projeto é aberto e precisa de ajuda, principalmente de quem pode testar em
outro ambiente ou revisar as partes sensíveis do código.

- [CONTRIBUTING.md](CONTRIBUTING.md): como relatar um erro, sugerir uma
  melhoria ou enviar código. Regra sem exceção: **nenhum dado real** em issues,
  pull requests ou testes.
- [ROADMAP.md](ROADMAP.md): o que está planejado e o que está fora do escopo.
- [Issues abertas](https://github.com/gomesmarcusvinicius/office-chat-keeper/issues):
  as marcadas com `good first issue` são boas para começar.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): o combinado de convivência.

## Documentação

- [docs/como-funciona.md](docs/como-funciona.md): cada parte da ferramenta e os
  conceitos por trás (LevelDB, IndexedDB, snapshot, arquivo permanente, busca,
  agendamento, verificação).
- [docs/restauracao.md](docs/restauracao.md): as duas estratégias de
  restauração, suas travas e seus limites.
- [docs/desenvolvimento.md](docs/desenvolvimento.md): como rodar os testes,
  gerar o pacote e publicar uma versão.
- [SECURITY.md](SECURITY.md): como relatar uma vulnerabilidade.
- [CHANGELOG.md](CHANGELOG.md): histórico de versões.

## Licença

[MIT](LICENSE). O leitor de IndexedDB/LevelDB vem embutido e é da CCL Solutions,
também sob licença MIT; veja
[src/office_chat_keeper/_vendor/README.md](src/office_chat_keeper/_vendor/README.md).
