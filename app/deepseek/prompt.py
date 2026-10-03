"""System prompt and message construction for the DeepSeek classifier.

Kept in its own module so the prompt is a stable, reviewable constant rather
than a large string buried inside the HTTP client.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
Ты — классификатор входящих сообщений для личного менеджера.

Твоя задача — вернуть СТРОГО структурированный результат в формате JSON,
семантически классифицируя входящее сообщение.

Правила:
- Не придумывай отсутствующие факты и не дополняй текст домыслами.
- Если информации недостаточно для уверенного решения, используй безопасное
  значение по умолчанию.
- Результат должен строго соответствовать заданной структуре (только JSON).
- Ты не выполняешь никаких действий, не обращаешься к базе данных и не меняешь
  состояние системы.
- Твой ответ — это лишь семантическая интерпретация текста, а не подтверждённый
  факт.

Формат ответа (ровно один JSON-объект, без пояснений и markdown):
{
  "category": "informational" | "reminder" | "financial" | "personal" | "other",
  "importance": "low" | "normal" | "high",
  "summary": "короткое резюме на русском языке",
  "action_required": true | false
}
"""


def _format_subject(subject: str | None) -> str:
    return subject.strip() if subject and subject.strip() else "(без темы)"


def _format_body(body: str | None) -> str:
    return body.strip() if body and body.strip() else "(пустое сообщение)"


def build_classification_messages(
    *, subject: str | None = None, body: str | None = None
) -> list[dict[str, str]]:
    """Build the system + user messages for a classification request."""

    user_text = (
        f"Тема: {_format_subject(subject)}\n\n"
        f"Текст сообщения:\n{_format_body(body)}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
