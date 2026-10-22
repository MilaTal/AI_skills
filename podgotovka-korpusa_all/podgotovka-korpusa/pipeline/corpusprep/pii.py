# -*- coding: utf-8 -*-
"""Поиск персональных данных в тексте фрагмента.

Проверка расширялась трижды, и каждый раз находка была случайной — глазами в
очередном разборе. Сначала ловились только полные ФИО, потом добавились личная
рабочая почта, обрезанные фамилии вида «Соболева Све...» и телефоны. Поэтому
список шаблонов здесь — накопленный опытом, а не выведенный заранее.
"""
import io
import os
import re

FIO = re.compile(r'\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна)\b')
INITIALS_AFTER = re.compile(r'\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.')
INITIALS_BEFORE = re.compile(r'\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}\b')
TRUNCATED = re.compile(r'\b[А-ЯЁ][а-яё]{3,}\s+[А-ЯЁ][а-яё]{1,4}\.{2,3}')
EMAIL = re.compile(r'\b[A-Za-z][A-Za-z0-9._-]{2,}@[A-Za-z0-9.-]+\.[a-z]{2,}\b')
PASSWORD = re.compile(r"(?i)(?:пароль|password|пасс|pwd)\s*[:=\-]?\s*[«\"']?([A-Za-z0-9][A-Za-z0-9!@#$%^&*_.\-]{3,})[»\"']?")
PHONE = re.compile(r'(?<!\d)\+?7\s?\(?\d{3}\)?\s?\d{3}[- ]?\d{2}[- ]?\d{2}(?!\d)')

# «Фамилия И.О.» — не имя, а требование инструкции к формату поля «Кому».
STUB = ('Иванов И.И.', 'И.И. Иванов', 'Иванов И. И.', 'Фамилия И.О.')
SERVICE_MAIL = ('info@', 'support@', 'supportaj@', 'noreply@', 'mail@')


def _digits(s):
    return re.sub(r'\D', '', s or '')


def _allowed_phones(cfg):
    """Телефоны компании — не персональные данные: это общий номер и факс из
    подписи в инструкции, они полезны в базе. Добавочный, указывающий на
    конкретного сотрудника, вычищается отдельно на этапе извлечения."""
    v = (cfg or {}).get('allow_phones') or ''
    if isinstance(v, str):
        v = [x for x in re.split(r'[;,]', v) if x.strip()]
    return {_digits(x) for x in v}


def _extra(cfg):
    p = (cfg or {}).get('pii_names')
    if not p or not os.path.exists(p):
        return []
    return [x.strip() for x in io.open(p, encoding='utf-8-sig') if x.strip() and not x.startswith('#')]


ZHURNAL = 'на-подтверждение.csv'


def zapisat_nahodku(cfg, etap, gde, nahodki):
    """Записать найденное в рабочую папку корпуса — и вернуть, сколько записано.

    Разделение простое и держится кодом, а не памятью: САМИ ЗНАЧЕНИЯ остаются
    здесь, на диске внутри компании; наружу — в машинный отчёт, в n8n, в таблицу
    очереди и в телеграм — уходит только «где и сколько».

    Иначе получается то, ради чего всё и затевалось, только наоборот: проверка
    находит настоящее имя и сама же выносит его в Google-таблицу и в чат.

    Файл открывается оператором. Дальше у него два ответа: имя настоящее —
    внести его в pii-names.txt, и вычистка уберёт его на следующем прогоне; имя
    ложное («Россия Росма...» — обрезанное название фирмы в ячейке) — пометить
    в последнем столбце и больше не возвращаться.
    """
    if not nahodki:
        return 0
    papka = os.path.join(cfg.get('work_dir', '.'), 'meta')
    os.makedirs(papka, exist_ok=True)
    put = os.path.join(papka, ZHURNAL)
    novyy = not os.path.exists(put)
    with io.open(put, 'a', encoding='utf-8-sig', newline='') as f:
        if novyy:
            f.write('этап;место;найдено;решение оператора\n')
        for n in nahodki:
            f.write('%s;%s;%s;\n' % (etap, gde, str(n).replace(';', ',')))
    return len(nahodki)


def find(text, cfg=None):
    """Возвращает список подозрительных мест. Пустой список — чисто.

    Заглушка «Иванов И.И.» вырезается до проверки: иначе она сама попадает под
    шаблон «инициалы плюс фамилия» вместе со следующим словом — «И.И.
    Согласовано» выглядит как имя, хотя это уже обезличенный текст.
    """
    for stub in STUB:
        text = text.replace(stub, ' ')
    hits = []
    for rx in (FIO, INITIALS_AFTER, INITIALS_BEFORE, TRUNCATED):
        for m in rx.findall(text):
            if any(s in m for s in STUB):
                continue
            hits.append(m.strip())
    # Телефоны и пароли убираются полностью, без исключений.
    hits.extend(PHONE.findall(text))
    for m in PASSWORD.finditer(text):
        if m.group(1) not in ('ХХХ', 'XXX') and m.group(1).lower() != 'указывается':
            hits.append('пароль: ' + m.group(1))
    for m in EMAIL.findall(text):
        low = m.lower()
        if not low.startswith(SERVICE_MAIL) and not low.startswith('ivanov'):
            hits.append(m)
    for name in _extra(cfg):
        if name in text:
            hits.append(name)
    return sorted(set(hits))
