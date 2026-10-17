"""Проверки допуска каталога — закрытый именованный список (DRF-2888).

Допуск кандидата к рекомендации считают восемь проверок. До этого модуля они
жили в S1 цепочкой ``if``, а три юридических (лицензия, адрес, квалификация)
схлопывались ещё в источнике в «первую несошедшуюся». Поэтому ответить на
вопрос владельца «что закрывает каждая проверка отдельно» было нечем: ответа
каждой проверки у кандидата не было.

Здесь проверки названы, упорядочены и отвечают **каждая**. «Первая
несошедшаяся» — уже свёртка поверх набора ответов (:func:`first_unmet`), и её
же считает S1. Перепись допуска в каталоге и шаг плана читают тот же набор,
а не свои копии правил: новая проверка появляется у всех сразу — новым
значением :class:`AdmissionCheck`.

### Что в список НЕ входит и не войдёт

Безопасность хода и здоровье, «предложение неактивно / мастер не умеет»,
совпадение с нуждой, бюджет, область запроса (включая «свои салоны»),
жёсткое «только X», видимость демо-салонов. Это не допуск каталога, и
диагностический прогон отключать их не вправе (решение владельца 07.10:
личность, права, изоляция салонов и согласия сохраняются).

### Исходы

``NOT_ENFORCED`` — не то же, что ``PASSED``: проверка существует, но сейчас
не действует (правило «неизвестное не допускается» выключено флагом
каталога). Свёртка по умолчанию такой ответ пропускает — иначе при
выключенном флаге закрылось бы всё, — но в «пройдено» он не превращается
нигде: потребитель обязан иметь возможность сказать «не проверялось».
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ._reason_codes import ReasonCode
from ._types import ConfigGate, LegalGate, MappingStatus


class AdmissionCheck(StrEnum):
    """Проверки допуска. Порядок объявления — порядок, в котором их читает S1."""

    #: Связь предложения с каноном подтверждена.
    MAPPING = "MAPPING"
    #: Канон не выведен из оборота.
    CANON_RETIRED = "CANON_RETIRED"
    #: Область классификации канона известна.
    SCOPE = "SCOPE"
    #: Конфигурация Body Care готова к скринингу.
    CONFIG = "CONFIG"
    #: Юридический класс канона подтверждён человеком.
    LEGAL_CLASS = "LEGAL_CLASS"
    #: Лицензия салона проверена и покрывает канон.
    LICENSE = "LICENSE"
    #: Место работы мастера подтверждено и указано в лицензии.
    ADDRESS = "ADDRESS"
    #: Квалификация мастера проверена.
    QUALIFICATION = "QUALIFICATION"


#: Все проверки, в порядке S1.
ALL_CHECKS: tuple[AdmissionCheck, ...] = tuple(AdmissionCheck)


class CheckOutcome(StrEnum):
    #: Проверка применялась и пройдена.
    PASSED = "PASSED"
    #: Применялась и не пройдена — в ответе код причины.
    FAILED = "FAILED"
    #: К этой строке не относится по существу, либо о том же уже сказала
    #: проверка раньше (класс не подтверждён — лицензию сверять нечем).
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Проверка существует, но сейчас не действует: правило «неизвестное не
    #: допускается» выключено. «Не проверено», а не «пройдено».
    NOT_ENFORCED = "NOT_ENFORCED"
    #: Ответа нет: ключа нет, незнакомое значение, сбой чтения. Закрывает.
    UNDETERMINED = "UNDETERMINED"
    #: Допущено для теста: строка — помеченные синтетические данные, читается
    #: под серверным разрешением тестового субъекта (DRF-2916, решение
    #: владельца 08.10). Бывает ТОЛЬКО у проверки связи и подменяет только её
    #: подтверждение: связь с каноном у строки есть, подтверждённой она быть
    #: не может по замку базы. Не ``PASSED`` и никогда им не станет.
    SYNTHETIC = "SYNTHETIC"


#: Исход — человеку: читающий отчёт или журнал не должен принять тестовый
#: допуск за настоящий.
SYNTHETIC_OUTCOME_LABEL = "Допущено для теста · синтетические данные"


#: Исходы, с которыми проверка не пройдена при любой свёртке.
UNMET_OUTCOMES = frozenset({CheckOutcome.FAILED, CheckOutcome.UNDETERMINED})


@dataclass(frozen=True)
class CheckAnswer:
    """Ответ одной проверки про одну строку каталога (и её мастера)."""

    check: AdmissionCheck
    outcome: CheckOutcome
    #: Код причины — тот же, что уходит на провод. Только при ``FAILED`` и
    #: ``UNDETERMINED``.
    reason: ReasonCode | None = None
    #: Что ответил читатель каталога: значение гейта или сырой литерал. Для
    #: отчёта оператора, на выбор не влияет.
    catalog_answer: str | None = None

    def __post_init__(self) -> None:
        if (self.outcome in UNMET_OUTCOMES) != (self.reason is not None):
            raise ValueError(
                f"{self.check}: код причины обязателен при FAILED / UNDETERMINED и запрещён иначе "
                f"(исход {self.outcome}, причина {self.reason})"
            )


def first_unmet(
    answers: Sequence[CheckAnswer],
    *,
    enabled: Iterable[AdmissionCheck] = ALL_CHECKS,
    not_enforced_is_unmet: bool = False,
) -> CheckAnswer | None:
    """Первая несошедшаяся из включённых проверок — так считает S1.

    ``enabled`` — какие проверки применять; по умолчанию все. Набор меньше
    полного — только диагностический прогон: на проводе такого параметра нет.

    ``not_enforced_is_unmet`` — строгая свёртка: проверка, которая сейчас не
    действует, тоже возвращается как несошедшаяся — **со своим исходом**
    ``NOT_ENFORCED``, а не с выдуманным ``FAILED``: причина у вызывающего
    будет «не проверялось», а не «не прошло».
    """
    wanted = frozenset(enabled)
    for answer in answers:
        if answer.check not in wanted:
            continue
        if answer.outcome in UNMET_OUTCOMES:
            return answer
        if not_enforced_is_unmet and answer.outcome is CheckOutcome.NOT_ENFORCED:
            return answer
    return None


# -- литералы каталога «проверка не действует» (services.body_care_scope) ----

#: Какой литерал каталога о какой проверке говорит. Закрытый набор: узел
#: сверяет его с тем, что отдаёт каталог.
UNENFORCED_LITERALS: dict[str, AdmissionCheck] = {
    "scope": AdmissionCheck.SCOPE,
    "legal_class": AdmissionCheck.LEGAL_CLASS,
    "license": AdmissionCheck.LICENSE,
    "address": AdmissionCheck.ADDRESS,
    "qualification": AdmissionCheck.QUALIFICATION,
}


# -- коды причин -----------------------------------------------------------

_CONFIG_REASON = {
    ConfigGate.UNCLASSIFIED: ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED,
    ConfigGate.NOT_READY: ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY,
}

_LEGAL_REASON = {
    LegalGate.CLASS_UNCONFIRMED: ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED,
    LegalGate.LICENSE_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED,
    LegalGate.LICENSE_SCOPE_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH,
    LegalGate.LOCATION_UNKNOWN: ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN,
    LegalGate.ADDRESS_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_ADDRESS_MISMATCH,
    LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED: ReasonCode.ELIG_EXCLUDED_QUALIFICATION_REQUIREMENT_UNCONFIRMED,
    LegalGate.QUALIFICATION_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
}

_UNDETERMINED = ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED

#: Отказы, которые принадлежат лицензии; адрес при них свою сверку не ведёт.
_LICENSE_FAILED = frozenset({LegalGate.LICENSE_NOT_VERIFIED, LegalGate.LICENSE_SCOPE_MISMATCH})


def _answer(check: AdmissionCheck, outcome: CheckOutcome, *, reason=None, catalog_answer=None) -> CheckAnswer:
    return CheckAnswer(check, outcome, reason, None if catalog_answer is None else str(catalog_answer))


def _legal_answer(
    check: AdmissionCheck,
    gate: LegalGate | None,
    *,
    verified: bool,
    unenforced: frozenset[AdmissionCheck],
    catalog_answer: str | None,
) -> CheckAnswer:
    """Ответ одной из трёх проверок §7A по её значению гейта.

    ``CLASS_UNCONFIRMED`` здесь — «не применима»: о классе отвечает проверка
    ``LEGAL_CLASS``, и одна причина не должна выглядеть тремя.
    """
    shown = catalog_answer if catalog_answer is not None else (None if gate is None else gate.value)
    if gate is None or gate is LegalGate.CLASS_UNCONFIRMED:
        return _answer(check, CheckOutcome.NOT_APPLICABLE, catalog_answer=shown)
    if gate is LegalGate.UNDETERMINED:
        return _answer(check, CheckOutcome.UNDETERMINED, reason=_UNDETERMINED, catalog_answer=shown)
    if gate is not LegalGate.CLEARED:
        return _answer(
            check, CheckOutcome.FAILED, reason=_LEGAL_REASON.get(gate, _UNDETERMINED), catalog_answer=shown,
        )
    if verified:
        return _answer(check, CheckOutcome.PASSED, catalog_answer=shown)
    if check in unenforced:
        return _answer(check, CheckOutcome.NOT_ENFORCED, catalog_answer=shown)
    return _answer(check, CheckOutcome.NOT_APPLICABLE, catalog_answer=shown)


def build_answers(
    *,
    mapping_status: MappingStatus,
    canon_retired: bool | None,
    config_gate: ConfigGate | None,
    license_gate: LegalGate | None,
    address_gate: LegalGate | None,
    qualification_gate: LegalGate | None,
    has_canon: bool = True,
    has_master: bool = True,
    license_verified: bool = False,
    address_verified: bool = False,
    qualification_verified: bool = False,
    address_waits_for_license: bool = False,
    unenforced: Iterable[AdmissionCheck] = (),
    synthetic: bool = False,
) -> tuple[CheckAnswer, ...]:
    """Восемь ответов про одну строку каталога — из значений гейтов.

    Чистая функция без базы: её зовёт и источник подбора (по уже загруженным
    строкам), и читатель по тройкам ``users.admission``. Одно место, где
    значение гейта становится ответом проверки.

    ``canon_retired=None`` и ``config_gate=None`` — у строки нет канонической
    связи (легаси): проверка канона к ней не относится. ``license_gate=None``
    — то же для юридических. ``has_canon=False`` — у предложения нет канона:
    класса у него быть не может, и это отказ проверки класса, а не её обход.

    ``*_verified`` различают «проверено» и «не требуется» у гейта ``CLEARED``:
    первое — ``PASSED``, второе — ``NOT_APPLICABLE`` (или ``NOT_ENFORCED``,
    если каталог говорит, что проверка сейчас не действует).

    ``address_waits_for_license`` — каталог ответил про адрес «сверять не с
    чем: нет покрывающей лицензии». Если лицензия при этом не сошлась, адрес
    «не применим»; если сошлась — два чтения разошлись, и это не толкуется.

    ``synthetic`` — строка помечена синтетикой И прочитана под действующим
    серверным разрешением (это решает вызывающий через
    ``services.synthetic``, не эта функция). Тогда проверка связи отвечает
    ``SYNTHETIC`` — при двух условиях сразу: канон у строки есть и связь в
    состоянии «есть, не подтверждена». Без канона, «не связана» и «решено не
    рекомендовать» — обычный отказ. Остальных семи проверок признак не
    касается вовсе.
    """
    off = frozenset(unenforced)
    out: list[CheckAnswer] = []

    # П1 — связь.
    if mapping_status is MappingStatus.VERIFIED:
        out.append(_answer(AdmissionCheck.MAPPING, CheckOutcome.PASSED, catalog_answer=mapping_status.value))
    elif synthetic and has_canon and mapping_status is MappingStatus.REVIEW_REQUIRED:
        # Единственное исключение для синтетики (DRF-2916): подтверждение связи.
        out.append(_answer(AdmissionCheck.MAPPING, CheckOutcome.SYNTHETIC, catalog_answer=mapping_status.value))
    else:
        out.append(_answer(
            AdmissionCheck.MAPPING, CheckOutcome.FAILED,
            reason=ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE, catalog_answer=mapping_status.value,
        ))

    # Канон выведен.
    if canon_retired is None:
        out.append(_answer(AdmissionCheck.CANON_RETIRED, CheckOutcome.NOT_APPLICABLE))
    elif canon_retired:
        out.append(_answer(
            AdmissionCheck.CANON_RETIRED, CheckOutcome.FAILED,
            reason=ReasonCode.ELIG_EXCLUDED_CANON_RETIRED, catalog_answer="retired",
        ))
    else:
        out.append(_answer(AdmissionCheck.CANON_RETIRED, CheckOutcome.PASSED))

    # П2 — область; П3 — конфигурация. Оба ответа несёт один гейт.
    shown = None if config_gate is None else config_gate.value
    if config_gate is None:
        scope = _answer(AdmissionCheck.SCOPE, CheckOutcome.NOT_APPLICABLE)
        config = _answer(AdmissionCheck.CONFIG, CheckOutcome.NOT_APPLICABLE)
    elif config_gate is ConfigGate.UNCLASSIFIED:
        scope = _answer(
            AdmissionCheck.SCOPE, CheckOutcome.FAILED,
            reason=_CONFIG_REASON[ConfigGate.UNCLASSIFIED], catalog_answer=shown,
        )
        config = _answer(AdmissionCheck.CONFIG, CheckOutcome.NOT_APPLICABLE, catalog_answer=shown)
    elif config_gate in (ConfigGate.NOT_SUBJECT, ConfigGate.READY, ConfigGate.NOT_READY):
        scope = _answer(
            AdmissionCheck.SCOPE,
            CheckOutcome.NOT_ENFORCED if AdmissionCheck.SCOPE in off else CheckOutcome.PASSED,
            catalog_answer=shown,
        )
        if config_gate is ConfigGate.NOT_SUBJECT:
            config = _answer(AdmissionCheck.CONFIG, CheckOutcome.NOT_APPLICABLE, catalog_answer=shown)
        elif config_gate is ConfigGate.READY:
            config = _answer(AdmissionCheck.CONFIG, CheckOutcome.PASSED, catalog_answer=shown)
        else:
            config = _answer(
                AdmissionCheck.CONFIG, CheckOutcome.FAILED,
                reason=_CONFIG_REASON[ConfigGate.NOT_READY], catalog_answer=shown,
            )
    else:
        # ``UNDETERMINED`` и любое значение, которого здесь не знают: ответа
        # про область нет, а без него про конфигурацию говорить нечего.
        scope = _answer(AdmissionCheck.SCOPE, CheckOutcome.UNDETERMINED, reason=_UNDETERMINED, catalog_answer=shown)
        config = _answer(AdmissionCheck.CONFIG, CheckOutcome.NOT_APPLICABLE, catalog_answer=shown)
    out.extend((scope, config))

    # П4 — юридический класс.
    if license_gate is None and address_gate is None and qualification_gate is None:
        out.append(_answer(AdmissionCheck.LEGAL_CLASS, CheckOutcome.NOT_APPLICABLE))
    elif (
        not has_canon
        or license_gate is LegalGate.CLASS_UNCONFIRMED
        or address_gate is LegalGate.CLASS_UNCONFIRMED
        or qualification_gate is LegalGate.CLASS_UNCONFIRMED
    ):
        out.append(_answer(
            AdmissionCheck.LEGAL_CLASS, CheckOutcome.FAILED,
            reason=_LEGAL_REASON[LegalGate.CLASS_UNCONFIRMED], catalog_answer=LegalGate.CLASS_UNCONFIRMED.value,
        ))
    elif AdmissionCheck.LEGAL_CLASS in off:
        out.append(_answer(AdmissionCheck.LEGAL_CLASS, CheckOutcome.NOT_ENFORCED))
    else:
        out.append(_answer(AdmissionCheck.LEGAL_CLASS, CheckOutcome.PASSED))

    # П5 — лицензия.
    out.append(_legal_answer(
        AdmissionCheck.LICENSE, license_gate, verified=license_verified, unenforced=off, catalog_answer=None,
    ))

    # П6 — адрес мастера.
    if not has_master:
        out.append(_answer(AdmissionCheck.ADDRESS, CheckOutcome.NOT_APPLICABLE))
    elif address_waits_for_license and license_gate in _LICENSE_FAILED:
        out.append(_answer(AdmissionCheck.ADDRESS, CheckOutcome.NOT_APPLICABLE, catalog_answer="no_covering_license"))
    else:
        out.append(_legal_answer(
            AdmissionCheck.ADDRESS, address_gate, verified=address_verified, unenforced=off,
            catalog_answer="no_covering_license" if address_waits_for_license else None,
        ))

    # П7 — квалификация мастера. Без канона спрашивать нечем — о том, что
    # канона нет, уже сказала проверка класса.
    if not has_master or not has_canon:
        out.append(_answer(AdmissionCheck.QUALIFICATION, CheckOutcome.NOT_APPLICABLE))
    else:
        out.append(_legal_answer(
            AdmissionCheck.QUALIFICATION, qualification_gate, verified=qualification_verified, unenforced=off,
            catalog_answer=None,
        ))

    return tuple(out)


def answers_from_collapsed_gates(
    *,
    mapping_status: MappingStatus,
    canon_retired: bool | None,
    config_gate: ConfigGate | None,
    legal_gate: LegalGate | None,
) -> tuple[CheckAnswer, ...]:
    """Ответы для кандидата, чей источник отдал юридические условия ОДНИМ значением.

    Источник, не называющий ответ каждой проверки, отдаёт «первое
    несошедшееся». Из него восстанавливается ровно столько, сколько известно:
    несошедшаяся проверка названа, а про те, что стоят ПОСЛЕ неё, ответа нет
    — они ``UNDETERMINED``. Для полной свёртки это ничего не меняет (первая
    несошедшаяся та же), а при диагностическом наборе закрывает — источнику
    без раздельных ответов нечем подтвердить, что остальные проверки прошли.
    """
    order = (
        (AdmissionCheck.LEGAL_CLASS, frozenset({LegalGate.CLASS_UNCONFIRMED})),
        (AdmissionCheck.LICENSE, _LICENSE_FAILED),
        (AdmissionCheck.ADDRESS, frozenset({LegalGate.LOCATION_UNKNOWN, LegalGate.ADDRESS_MISMATCH})),
        (
            AdmissionCheck.QUALIFICATION,
            frozenset({LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED, LegalGate.QUALIFICATION_NOT_VERIFIED}),
        ),
    )
    head = build_answers(
        mapping_status=mapping_status, canon_retired=canon_retired, config_gate=config_gate,
        license_gate=None, address_gate=None, qualification_gate=None,
    )[:4]
    legal: list[CheckAnswer] = []
    if legal_gate is None:
        legal = [_answer(check, CheckOutcome.NOT_APPLICABLE) for check, _ in order]
    elif legal_gate is LegalGate.CLEARED:
        legal = [_answer(check, CheckOutcome.PASSED, catalog_answer=legal_gate.value) for check, _ in order]
    else:
        failed_at = next((i for i, (_, gates) in enumerate(order) if legal_gate in gates), 0)
        for index, (check, _) in enumerate(order):
            if index < failed_at:
                legal.append(_answer(check, CheckOutcome.PASSED))
            elif index == failed_at and legal_gate is not LegalGate.UNDETERMINED:
                legal.append(_answer(
                    check, CheckOutcome.FAILED,
                    reason=_LEGAL_REASON.get(legal_gate, _UNDETERMINED), catalog_answer=legal_gate.value,
                ))
            else:
                legal.append(_answer(
                    check, CheckOutcome.UNDETERMINED, reason=_UNDETERMINED, catalog_answer=legal_gate.value,
                ))
    return (*head, *legal)
