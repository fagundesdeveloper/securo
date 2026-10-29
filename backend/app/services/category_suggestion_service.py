"""Ask a model which category an imported transaction belongs to.

Runs after the rules, on whatever they could not place, and writes into
`suggested_category_id` — a *suggestion*, which the import review screen
already renders and the user still has to accept. Nothing here categorizes
anything on its own.

Rules win on purpose. They are the user's own stated knowledge, they are
deterministic, and they cost nothing; the model is for the long tail they
were never going to cover.
"""
import json
import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config import get_agent_settings
from app.agents.providers.base import ChatMessage, LLMError
from app.agents.services import connection_service
from app.models.category import Category
from app.schemas.transaction import TransactionImport

logger = logging.getLogger(__name__)

# One request per chunk. Sized so a statement of a few hundred rows costs a
# handful of calls rather than one per row, while keeping each prompt small
# enough that the model does not lose track of the numbering.
_CHUNK = 60

# A ceiling on how much of one upload gets sent. A 5,000-row file would
# otherwise hold the preview request open for minutes; past this the rest
# simply arrives unsuggested, which is what used to happen to all of it.
_MAX_CHUNKS = 10

_PROMPT = """Você classifica lançamentos bancários brasileiros.

Categorias disponíveis (use EXATAMENTE um destes nomes):
{categories}

Para cada lançamento numerado abaixo, responda a categoria mais provável.

Regras:
- Responda SOMENTE um objeto JSON: {{"1": "Mercado", "2": "Transporte"}}
- Use apenas nomes da lista acima, copiados exatamente.
- Omita o número quando não tiver confiança razoável. Omitir é melhor que
  errar: o número omitido simplesmente chega sem sugestão, e errar faz o
  usuário desfazer.
- Entrada de dinheiro quase nunca é categoria de despesa.

Lançamentos:
{items}"""


def _parse_mapping(raw: str, valid: set[str]) -> dict[int, str]:
    """Pull {index: category} out of the reply, dropping anything unusable.

    Defensive by design: the model may wrap the object in prose or a code
    fence, invent a category, or number something that was never sent. Each
    of those is dropped rather than letting a guess through.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in data.items():
        if not isinstance(value, str) or value not in valid:
            continue
        try:
            out[int(str(key).strip())] = value
        except ValueError:
            continue
    return out


async def suggest_categories(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    transactions: list[TransactionImport],
) -> int:
    """Fill `suggested_category_id` on rows that still have none.

    Returns how many rows got a suggestion. Never raises: an import that
    cannot reach the model is an import without model suggestions, not a
    failed import.
    """
    if not get_agent_settings().enabled:
        return 0

    pending = [t for t in transactions if not t.suggested_category_id and not t.category_id]
    if not pending:
        return 0

    categories = list(
        (
            await session.execute(
                select(Category).where(Category.workspace_id == workspace_id)
            )
        ).scalars()
    )
    if not categories:
        return 0
    by_name = {c.name: c for c in categories}

    try:
        connection = await connection_service.get_default_connection(session, user_id)
        if connection is None:
            return 0
        # Reuse the agents' own builder: it already owns decrypting the key
        # and the base-url rules, and a second copy would drift from it.
        provider = connection_service.build_provider_for_connection(connection)
        model = connection.default_model
        if not model:
            return 0
    except Exception:  # noqa: BLE001
        logger.exception("category suggestion: could not build a provider")
        return 0

    filled = 0
    chunks = [pending[i:i + _CHUNK] for i in range(0, len(pending), _CHUNK)][:_MAX_CHUNKS]
    for chunk in chunks:
        items = "\n".join(
            f"{n}. {t.description} | {'entrada' if t.type == 'credit' else 'saida'} "
            f"R$ {t.amount}"
            for n, t in enumerate(chunk, start=1)
        )
        prompt = _PROMPT.format(
            categories="\n".join(f"- {c.name}" for c in categories), items=items
        )
        try:
            response = await provider.chat(
                [ChatMessage(role="user", content=prompt)],
                model=model,
                temperature=0.0,
                # Classification, not analysis: no budget for deliberation.
                thinking_budget=0,
            )
        except LLMError:
            logger.warning("category suggestion: provider refused a chunk", exc_info=True)
            continue
        except Exception:  # noqa: BLE001
            logger.exception("category suggestion: chunk failed")
            continue

        for index, name in _parse_mapping(response.content, set(by_name)).items():
            if not 1 <= index <= len(chunk):
                continue
            category = by_name[name]
            target = chunk[index - 1]
            target.suggested_category_id = category.id
            target.suggested_category_name = category.name
            filled += 1

    return filled
