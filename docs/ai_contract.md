# Контракт AI Core

API принимает JSON через POST на loopback. Поддерживаются прежние маршруты, параметры и поля. В реальном режиме тело может содержать закрытые материалы и остаётся в локальном контуре.

| Маршрут | Вход | Ответ |
|---|---|---|
| /api/analyze | query, config?, metrika? {start,end,segment?}, web? {product,domains,topics} | Отчёт расследования |
| /api/analysis/report | run_id | Сохранённый отчёт |
| /api/analysis/journal | {} | runs с датой, режимом, длительностью, кодами состояний и usage |
| /api/analysis/settings | {} | Готовность модели и разрешённые публичные значения |
| /api/tasks | {} | tasks, до 50 последних решений |
| /api/tasks/decide | run_id, proposal_id, action = accept или reject, idempotency_key, target для accept, control_min? | Серверная запись с фиксированными критериями |
| /api/tasks/review | task_id, mode = actual или simulation, observation? {fix,rows} | Статус, согласованные критерии, оценка и рекомендация |

Отчёт содержит прежние mode, model, hits, checks, notice, seconds, usage и при наличии hypothesis, proposal, refs. Расширение содержит run_id, llm_status (ok, unavailable, invalid_response), data_mode (synthetic, local), sources_status, evidence, summary, findings, hypotheses, recommended_actions, backlog_matches, proposed_tasks, effect_plan, limitations и trace. В findings и hypotheses ссылки evidence_ids и contradictions указывают на evidence[].id. Уровень confidence рассчитывается сервером. Числа появляются только в серверных расчётах. Ответ без гипотез и с валидной схемой сохраняется с llm_status=ok.

У evidence есть id, evidence_id, kind, ref, locator, text, unit, metric, filter, period, quality, data_mode, availability, observed_at, retrieved_at и необязательные values. Неизвестные значения остаются null. Тестовый счётчик отмечен demo_counter. Веб-сниппеты помечены snippet и не подтверждают изменение продаж.

Повтор решения с тем же ключом идемпотентен. Изменение payload при повторе отклоняется. Оценка использует первые четыре полные недели после исправления. Статусы waiting_for_data, achieved, partial, not_achieved, inconclusive. Режим simulation не записывает фактическую оценку.

Адаптер web_search вызывается лениво при явном разрешении. На входе только выбранные значения публичного каталога, доменов и тем. Результат соответствует согласованному контракту v1.
