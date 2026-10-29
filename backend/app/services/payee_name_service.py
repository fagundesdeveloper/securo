"""Recover a person's name from the bank's wording of a transfer.

Open Finance hands back the counterparty's document as the payee — a bare
CPF reads as nothing in a list of a hundred people — while the name itself
sits in the description the bank wrote. This reads it back out.

Deliberately regex over a model: the shapes are a short, closed set of bank
templates, and a wrong name on a financial counterparty is worse than no
name. Anything that does not match a known template is left alone.
"""
import re

# Bank templates, each capturing the name. Ordered most specific first; the
# first match wins. `\s{2,}` matters — several banks pad the name out to a
# fixed width and then append a date, so the run of spaces is the delimiter.
_TEMPLATES: tuple[re.Pattern, ...] = (
    # "PIX ENVIADO - DES  Maria de Souza Lima 01/05 - DOCTO: 1"
    re.compile(r"PIX\s+(?:ENVIADO|RECEBIDO)\s*-\s*(?:DES|REM)\s+(.+?)(?:\s{2,}|\s+\d{2}/\d{2}|\s+-\s+DOC|$)", re.I),
    # "TRANSF.AUTORIZ.ENTRE C/C - CARLOS EDUARDO ALVES - ..."
    re.compile(r"TRANSF[.\s]*AUTORIZ[^-]*-\s*(.+?)\s*(?:-|$)", re.I),
    # "Transferência Recebida|MARIA DE SOUZA LIMA"
    re.compile(r"Transfer[êe]ncia\s+(?:Recebida|Enviada)\s*\|\s*(.+?)\s*$", re.I),
    # "TED recebida TED 102.0001.JOANA R D"
    re.compile(r"TED\s+(?:recebida|enviada)\b.*?\d\.\d{4}\.(.+?)\s*$", re.I),
    # "Pix enviado ANA PEREIRA DOS SANTOS" / "PIX RECEBIDO CLEIDE 03/09"
    re.compile(r"PIX\s+(?:ENVIADO|RECEBIDO|ENVIADOS|RECEBIDOS)\s+(.+?)(?:\s{2,}|\s+\d{2}/\d{2}|$)", re.I),
    # "PAGTO ELETRON  COBRANCA - COLEGIO EXEMPLO LTDA - DOCTO: 148"
    re.compile(r"PAGTO\s+ELETRON\s+COBRANCA\s*-\s*(.+?)\s*(?:-\s*DOCTO|$)", re.I),
)

# A name is a name, not a bank verb, a document or a date fragment.
_NOT_A_NAME = re.compile(
    r"^(?:\d|pix\b|ted\b|doc\b|docto\b|transf|pagto|cobranca|cobrança)",
    re.I,
)


def extract_person_name(description: str | None) -> str | None:
    """Return the counterparty's name from a bank description, or None.

    None means "no template matched" — the caller keeps whatever it had,
    rather than storing a guess.
    """
    if not description:
        return None
    for pattern in _TEMPLATES:
        match = pattern.search(description)
        if not match:
            continue
        name = " ".join(match.group(1).split())
        # Drop a trailing date or document the template could not fence off.
        name = re.sub(r"\s*\d{2}/\d{2}(?:/\d{2,4})?\s*$", "", name).strip(" -|")
        if len(name) < 3 or _NOT_A_NAME.match(name):
            continue
        # A document is not a name, even when it sits where one should be.
        if re.fullmatch(r"[\d.\-/\s]+", name):
            continue
        return name
    return None


def looks_like_document(value: str | None) -> bool:
    """True for a payee whose whole name is a CPF or CNPJ."""
    if not value:
        return False
    stripped = value.strip()
    return bool(
        re.fullmatch(r"\d{3}\.\d{3}\.\d{3}-\d{2}", stripped)
        or re.fullmatch(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", stripped)
        or re.fullmatch(r"\d{11}", stripped)
        or re.fullmatch(r"\d{14}", stripped)
    )
