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

Отчёт содержит прежние mode, model, hits, checks, notice, seconds, usage и при наличии hypothesis, proposal, refs. Расширение содержит run_id, llm_status (ok, unavailable, invalid_response, not_needed), data_mode (synthetic, local), sources_status, evidence, summary, findings, hypotheses, recommended_actions, backlog_matches, proposed_tasks, effect_plan, limitations и trace. В findings и hypotheses ссылки evidence_ids и contradictions указывают на evidence[].id. Уровень confidence рассчитывается сервером. Числа появляются только в серверных расчётах. Ответ без гипотез и с валидной схемой сохраняется с llm_status=ok.

У evidence есть id, evidence_id, kind, ref, locator, text, unit, metric, filter, period, quality, data_mode, availability, observed_at, retrieved_at и необязательные values. Неизвестные значения остаются null. Тестовый счётчик отмечен demo_counter. Веб-сниппеты помечены snippet и не подтверждают изменение продаж.

Повтор решения с тем же ключом идемпотентен. Изменение payload при повторе отклоняется. Оценка использует первые четыре полные недели после исправления. Статусы waiting_for_data, achieved, partial, not_achieved, inconclusive. Режим simulation не записывает фактическую оценку.

Адаптер web_search вызывается лениво при явном разрешении. На входе только выбранные значения публичного каталога, доменов и тем. Результат соответствует согласованному контракту v1.

## Диагностика и экспорт

POST /api/analysis/check принимает config и запускает проверку порога. Поле notification показывает переход к новому сигналу для данного набора настроек. /api/analysis/export принимает run_id и возвращает только статусы, без выводов и доказательств. /api/tasks/redmine-export принимает task_id и возвращает локальный payload со статусом pending.

При наличии локального PM_LLM_URL расследование выполняет не более восьми решений планировщика. Каждый вызов выбирается один раз из оставшихся инструментов, получает параметры только из исходного запроса и возвращает новые evidence IDs. В журнале отмечены actor, tool, purpose, status, seconds и evidence_ids. Вызовы Метрики и публичного каталога требуют явно заданного оператором scope. При ошибке планировщика сервер достраивает локальные проверки. Устаревший PM_LLM_SELECT_STEP влияет только на прямой вызов прежней функции qwen.infer.

В запросе /api/analyze можно передать metrika.compare=true для двух полных календарных окон вместо start и end. Совпадение периодов не гарантирует соответствия товарного сегмента. task_id разрешает прочитать последнюю оценку согласованной задачи. Состояние not_needed означает отсутствие генерации гипотез для явно непрофильного запроса либо для вопроса о замере, где сервер установил отсутствие падения или недостаток наблюдений.

В выводах statement_type разделяет факт, предположение и гипотезу. evidence_ids содержат опору, contradictions отмечают конкурирующие объяснения, missing и checks описывают пробелы и следующий шаг. Числовые поля values рассчитываются сервером. Для нового согласования предусмотрен stop_if_below. В наблюдении можно передать external_factors. При согласовании guardrail_metrics задаёт минимальные значения дополнительных показателей. В наблюдении guardrails задаёт baseline, after, start и end для каждого из них. Неподтверждённый или нарушенный минимум оставляет оценку inconclusive. Зафиксированные внешние факторы или недоступный обязательный контроль оставляют оценку inconclusive.

POST /api/analysis/reopen принимает run_id и повторяет ранее сохранённый запрос. Результат содержит previous_run_id и evidence_changes. POST /api/analysis/daily возвращает отдельные сводки реальных и синтетических отчётов за день UTC. Журнал возвращает notifications с идентификаторами фоновых расследований и полями email_status, email_attempts, email_reason. Для прежних записей поля почты равны null. Статус submitted означает приём SMTP-сервером.
