# Histórico de versões

## Ainda não lançado

- `install-task --task-name <nome>`: a dica final de como desligar agora inclui
  o `--task-name`, com aspas quando o nome tem espaço. Antes, quem seguisse a
  dica tentava remover a tarefa de nome padrão. (Contribuição de
  HarshRajSinghania, pull request 9.)
- `--version` e o cabeçalho do `status` mostram, em uma linha, o aviso de
  projeto experimental feito por vibe coding.
- README: observação sobre o limite de 260 caracteres do Windows na instalação
  com `--user`.
- Preparação para contribuições: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `ROADMAP.md`, modelos de issue e de pull request.

## 0.1.0

Primeira versão pública. Experimental.

- **Descoberta e snapshot.** Localiza os armazenamentos `pivot.claude.ai` do
  Office e copia as pastas brutas, somente leitura, mesmo com o Office aberto.
  Só copia quando algo mudou, reaproveita arquivos iguais entre snapshots e
  aplica retenção.
- **Leitura e exportação.** Lê as conversas do IndexedDB e exporta cada uma em
  Markdown e JSON, com as imagens como arquivos.
- **Arquivo permanente.** SQLite só de acréscimo, com histórico de versões e
  alertas de possível perda de histórico.
- **Busca.** Índice de texto completo (FTS5), sem diferenciar maiúsculas nem
  acentos.
- **Backup automático.** Tarefa por usuário no Agendador de Tarefas, sem
  administrador e sem janela.
- **Verificação periódica.** Relatório a cada execução; em perda nova, plano de
  restauração em modo simulação e notificação do Windows.
- **Restauração (experimental).** Duas estratégias manuais: restaurar o
  snapshot bruto inteiro e inserir só as conversas que faltam. Testadas apenas
  em pastas e páginas de teste.
- Sem dependências externas: o leitor de IndexedDB/LevelDB (CCL Solutions, MIT)
  vai embutido.
