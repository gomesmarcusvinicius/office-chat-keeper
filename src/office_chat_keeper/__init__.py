"""office-chat-keeper: backup local do histórico dos suplementos do Claude no Office.

Ferramenta NÃO oficial, sem vínculo com a Anthropic nem com a Microsoft.
"""

__version__ = "0.1.1"

# Aviso de uma linha, mostrado em `--version` e no cabeçalho do `status`.
# "Vibe coding" = código escrito com ajuda de IA, sem revisão profissional.
NOTICE = "experimental, feito por vibe coding; não oficial"


def banner() -> str:
    """Nome, versão e o aviso, em uma linha só."""
    return f"office-chat-keeper {__version__} - {NOTICE}"
