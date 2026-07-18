# Knowledge schema amendment — v1.3

## Status

Review.

## Purpose

Разделить ответственность за документ, архитектурное владение системой и
расположение канонического источника, не делая существующий knowledge graph
мгновенно невалидным.

## Changes

1. `schema_version` повышена с `1.1` до `1.3`.
2. `system_owner` определён как список архитектурных систем или bounded systems.
3. Роли и команды остаются только в `owner`.
4. Git-репозитории вынесены в контролируемое поле `source_repository`.
5. Добавлены registry `system_owner`, `source_repository` и `term_maturity`.
6. Добавлен нормативный тип `terminology-standard`.
7. Добавлен временный профиль `system-owner-migration`:
   - legacy scalar выдаёт warning;
   - крайний срок миграции — 2026-08-15.
8. `to-be-confirmed` оставлен как временное migration-значение:
   - запрещён для approved, approved-with-amendments, implemented и delivered;
   - допустим только до 2026-08-15.

## Migration

Ручной mapping существующих knowledge nodes находится в
`docs/.knowledge/system-owner-migration.yaml`.

Миграция не использует массовую замену ролей на системы. Для каждого node
отдельно определены:

- `owner`;
- `system_owner`;
- при необходимости `source_repository`.

## Compatibility

Профиль `system-owner-migration` является временным. После проверки и миграции
всех активных узлов он удаляется отдельным amendment.

## Related documents

- [[Ayla Knowledge Architecture Specification v1.2]]
- [[Glossary|Ayla Glossary v1.2]]
- [[Knowledge Schema Reference]]
