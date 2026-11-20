# Para onde o projeto vai

Este é um plano de intenções, não uma promessa. O projeto é mantido por uma
pessoa, nas horas vagas, e as prioridades podem mudar. Itens marcados com
**(ajuda)** dependem de alguém com outro ambiente ou com mais experiência.

O estado atual, com o que foi e o que não foi testado, está em
"Limitações conhecidas" no [README](README.md).

## Agora: ganhar confiança no que já existe

- **Testar nos outros apps (ajuda).** Confirmar a leitura no Word, no
  PowerPoint e no Outlook. Hoje os nomes internos desses apps são suposições.
- **Testar em outras máquinas (ajuda).** Outras versões do Office e do
  suplemento, Windows 10, mais de uma conta na mesma máquina.
- **Revisão de segurança (ajuda).** Filtro de credenciais das exportações,
  travas da restauração e leitura de arquivos binários malformados. Os pontos
  estão no [SECURITY.md](SECURITY.md).
- **Instalação mais robusta.** Encurtar os caminhos internos do pacote, para
  não esbarrar no limite de 260 caracteres do Windows.

## Depois: restauração de verdade

- **Validar a restauração em mais ambientes (ajuda).** A estratégia A funcionou
  uma vez em uso real (Excel e PowerPoint, uma conta). Falta a estratégia B em
  uso real, e as duas em outras máquinas e versões do Office.
- **Alertar quando um registro não puder ser lido.** Hoje isso só aparece no
  log; foi assim que um defeito de leitura passou dias sem ser notado.
- **Testes automatizados do script de restauração seletiva.** Hoje o
  `restore_script.js` é conferido só de forma estática.
- **Acompanhar mudanças de esquema.** Detectar cedo quando uma atualização do
  suplemento muda o formato, e avisar com clareza em vez de falhar.

## Mais adiante: conforto de uso

- Documentação e mensagens em inglês.
- Exportação para outros formatos (HTML, um arquivo único por conversa).
- Uma tela simples para navegar e buscar nas conversas guardadas.
- Instalador que não exija conhecer o Prompt de Comando.

## Fora do escopo

- **macOS e Office na web.** O armazenamento é outro; seria outro projeto.
- **Sincronização ou backup em nuvem.** A ferramenta é 100% local, de
  propósito.
- **Qualquer envio de dados** (telemetria, relatório automático de erro).
- **Restauração automática.** Escrever na pasta do Office é sempre manual e
  com confirmação.

## Como influenciar este plano

Abra uma issue com o modelo "Sugestão de melhoria" ou comente em uma issue
existente. Veja o [CONTRIBUTING.md](CONTRIBUTING.md).
