# Restauração

> **Experimental.** Restaurar dentro do suplemento não é suportado oficialmente e pode deixar de funcionar com uma atualização. A estratégia A foi usada uma vez no armazenamento real do Office, com sucesso: as conversas voltaram e o suplemento as exibiu. A estratégia B foi testada apenas em páginas de teste. O caminho seguro é sempre anexar o Markdown exportado a uma conversa nova.

A ferramenta nunca restaura sozinha. Quando detecta uma perda, ela grava um plano de restauração em modo simulação (veja "Verificação periódica" em [como-funciona.md](como-funciona.md)). As estratégias abaixo só acontecem se você as executar e confirmar.

## Estratégia A: restaurar o snapshot bruto inteiro

```
office-chat-keeper restore-snapshot                          (lista os snapshots)
office-chat-keeper restore-snapshot <fonte>/<snapshot>       (SIMULA; não altera nada)
office-chat-keeper restore-snapshot <fonte>/<snapshot> --execute
```

`restore-snapshot` é o **único** comando que escreve na pasta do Office. Ele
nunca roda sozinho (a tarefa agendada não o chama) e, sem `--execute`, só
mostra o que mudaria.

**O que faz.** Troca as pastas `.leveldb` e `.blob` de uma fonte pelo conteúdo
de um snapshot. É a restauração mais fiel (os arquivos voltam byte a byte), mas
é tudo ou nada: o banco inteiro volta ao instante do snapshot, inclusive skills
e conectores, e conversas mais novas saem do Office (continuam no arquivo
permanente).

**Por que trocar a pasta, e não editar o banco.** Um banco LevelDB só é
consistente como conjunto de arquivos; alterar um arquivo por conta própria o
corromperia. A ferramenta nunca edita arquivos do banco: troca a pasta inteira
por outra que já era um banco válido.

**As travas.** A execução é recusada se qualquer uma falhar:

| Trava | Como é verificada |
|---|---|
| Snapshot íntegro | SHA-256 de cada arquivo contra o manifesto |
| Office fechado | Excel, Word, PowerPoint e Outlook não podem estar em execução |
| Banco livre | O arquivo `LOCK` precisa abrir com acesso exclusivo; se algum processo o usa, o Windows recusa |
| Estado atual no backup | A fonte precisa estar igual ao último snapshot, já ingerido no arquivo |
| Mesmo esquema | Bancos, versões, chaves e índices do snapshot iguais aos da fonte. Um banco que existe só no snapshot vira aviso (o suplemento cria alguns sob demanda, e a troca o traz junto) |
| Confirmação | Você digita `RESTAURAR <id da fonte>`; sem terminal interativo, recusa |

**A troca, e por que é reversível.**

1. Copia o estado atual para `pre-restore\<fonte>-<data>` e confere a cópia.
2. Copia o snapshot para uma pasta provisória ao lado do destino e confere os
   hashes. Até aqui o banco em uso segue intacto.
3. Renomeia a pasta atual para um nome provisório e a nova para o nome
   definitivo. Renomear é instantâneo e reversível, e o Windows recusa renomear
   uma pasta que tenha arquivo aberto.
4. Relê o banco restaurado e compara as conversas com as do snapshot.
5. Se algo falhar nos passos 3 ou 4, desfaz sozinho: tira o que pôs e devolve o
   que havia. Só depois de tudo conferido as pastas antigas são removidas.

A cópia de segurança do passo 1 fica guardada. Para desfazer uma restauração
depois, restaure essa cópia (o comando exato fica em
`reports\restauracao-<data>.md`).

**Limites conhecidos.**

- Se o suplemento atualizou o esquema do banco depois do snapshot, a trava de
  esquema impede a estratégia A. Restam a estratégia B e o caminho seguro.
  Exceção: um banco que existe só no snapshot (o suplemento cria alguns sob
  demanda, como `claude-office-snipped-results`) não impede; vira aviso.
- No uso real, o suplemento listou as conversas restauradas só a partir do
  IndexedDB. Isso valeu para a versão do Office e do suplemento de novembro de
  2026 e pode mudar.
- Restaurar em uma pasta diferente da original exige `--target <id>` e pode não
  funcionar se a identidade (usuário ou organização) for outra.

## Estratégia B: inserir só as conversas que faltam

```
office-chat-keeper restore-script                       (conversas que faltam na fonte)
office-chat-keeper restore-script --conversation 1a2b   (uma conversa específica, para ensaio)
```

A estratégia A troca o banco inteiro. A B devolve só o que falta, mantendo as
conversas novas: quem grava é o próprio navegador, pela API `indexedDB`, com o
suplemento aberto. Nenhum arquivo do LevelDB é tocado.

O comando **não grava nada no Office**. Ele monta, em `restore\<data>\`, um
pacote com quatro arquivos:

| Arquivo | O que é |
|---|---|
| `dados-restauracao.json` | Os registros brutos das conversas, lidos do snapshot, e o esquema do banco |
| `script-para-colar-no-console.txt` | O script que **você** cola no Console do DevTools do painel do Claude |
| `ensaio.html` | Página local que cria um banco descartável de mesmo esquema, para ensaiar |
| `PASSOS.md` | O passo a passo, começando pelo ensaio |

**Por que um formato próprio de arquivo.** O registro de uma conversa é um
objeto JavaScript, e o JSON não representa tudo o que o JavaScript tem:
`undefined`, datas e binários não existem em JSON. Para a conversa voltar
idêntica, esses valores vão marcados (`{"$t": "date", "v": 1793...}`). O script
desfaz a marcação ao gravar e a refaz para conferir.

**O que o script faz, na ordem.**

1. Abre um painel na página. Você escolhe o arquivo de dados (leitura local).
2. Confere o esquema vivo: versão do banco, stores, chaves e índices. Se algo
   diferir do arquivo, para.
3. Mostra a simulação: o que seria inserido, o que já existe (e não seria
   tocado) e se `userId`/`orgId` seriam ajustados. O ajuste só é proposto
   quando o banco tem uma única identidade e ela difere da conversa.
4. Só depois de você digitar `GRAVAR`, grava tudo em uma única transação.
5. Relê cada registro e compara com o arquivo. Se não conferir, remove apenas
   o que ele mesmo inseriu.

**O que o script não faz.** Não acessa a rede. Não cria o banco nem muda o
esquema. Não usa `put()`, que sobrescreveria: usa `add()`, que falha se a chave
já existir, e nesse caso a transação inteira é desfeita. O arquivo
`script-para-colar-no-console.txt` é curto e comentado para você ler antes de colar; os testes
conferem que ele não contém chamadas de rede, `eval` nem `put()`.

**Sem porta de depuração.** O caminho é manual, pelo DevTools que o próprio
painel abre (Ctrl+Shift+I). Nenhuma porta fica escutando e nenhum programa
externo controla a página do suplemento.

**Limites conhecidos.**

- O script JavaScript não roda nos testes automatizados (o pytest não tem
  navegador); ali ele é conferido só de forma estática. O ensaio em navegador
  foi feito à mão, com dados sintéticos e com uma conversa real de 43 MB.
- O desfazer por falha de conferência não foi exercitado no navegador.
- Valores `BigInt`, `Map`, `Set` e `RegExp` não são distinguidos pelo leitor.
  Uma conversa com inteiro grande demais fica fora do pacote, com o motivo.
- O pacote contém o conteúdo das conversas. Fica na pasta de dados protegida;
  não o compartilhe.
