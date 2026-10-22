# -*- coding: utf-8 -*-
"""Проверка готового файла фрагментов.

Пункты из требований плюс те, что добавились после реальных случаев: залипание
модели (ответ формально валиден и при этом бессмыслен), персональные данные,
сокращения вне известного списка.

Отдельно про последнюю. Проверка на «не пусто» и «нет ФИО» ловит не всё: модель
может вернуть километр одного символа, и такой ответ пройдёт обе. Поэтому
проверок несколько и каждая узкая.
"""
import io
import json
import os
import re
from collections import Counter

from . import pii
from .util import read_csv, Report

FIELDS = ['chunk_id', 'chunk_text', 'source_id', 'title', 'document_type', 'topic',
          'version', 'status', 'source_url', 'updated_at', 'backup_url', 'section',
          'doc_subtype', 'system', 'source_file', 'screen_years', 'has_screen',
          'block_id', 'block_title', 'ticket', 'doc_date', 'stage']
# Номер обращения и этап есть не у всех: практика нумерации появилась примерно с
# конца 2025 года, и у четверти блоков её нет. Пустое поле здесь — факт, а не
# недоработка разбора.
OPTIONAL = ('doc_subtype', 'screen_years', 'ticket', 'stage', 'doc_date')
DEGENERATE = re.compile(r'(.)\1{40,}')
ABBR = re.compile(r'\b[А-ЯЁ]{2,5}\b')
LATIN = re.compile(r'\b[A-Z]{2,5}\b')


def known_abbr(cfg):
    p = cfg.get('abbreviations')
    out = set()
    if p and os.path.exists(p):
        for line in io.open(p, encoding='utf-8-sig'):
            line = line.strip()
            if line and not line.startswith('#'):
                out.add(line.split('—')[0].strip())
    return out


def validate(cfg, path=None, report=None):
    report = report or Report('validate')
    path = path or cfg['out']
    raw = io.open(path, encoding='utf-8').read()
    if raw.startswith('﻿'):
        report.problem('файл начинается с BOM')
    if '\r\n' in raw:
        report.problem('перевод строки CRLF вместо LF')

    rows = []
    for i, line in enumerate(raw.split('\n'), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception as e:
            report.problem('строка %d не разбирается как JSON: %s' % (i, e))

    system = cfg['system']
    for r in rows:
        cid = r.get('chunk_id', '?')
        miss = [f for f in FIELDS if f not in r]
        if miss:
            report.problem('%s: нет полей %s' % (cid, miss))
        extra = [f for f in r if f not in FIELDS]
        if extra:
            report.problem('%s: лишние поля %s' % (cid, extra))
        if r.get('system') != system:
            report.problem('%s: system = %r' % (cid, r.get('system')))
        if not (r.get('chunk_text') or '').strip():
            report.problem('%s: пустой chunk_text' % cid)
        for f in FIELDS:
            v = r.get(f)
            if isinstance(v, str) and 'ЗАПОЛНИТЬ' in v:
                report.problem('%s: заглушка ЗАПОЛНИТЬ в поле %s' % (cid, f))
            if f not in OPTIONAL and v in ('', None):
                report.problem('%s: пустое обязательное поле %s' % (cid, f))
        if DEGENERATE.search(r.get('chunk_text', '')):
            report.problem('%s: залипший текст' % cid)
        leak = pii.find(r.get('chunk_text', ''), cfg)
        if leak:
            # Не замечание конвейера, а вопрос к человеку — и без значений:
            # этот отчёт уезжает в n8n, в таблицу очереди и в телеграм.
            # Место называем документом и заголовком, а не одним только
            # хэшем фрагмента: оператору искать руками, а хэш ничего не
            # подсказывает о том, куда смотреть.
            gde = '%s «%s» фрагмент %s' % (r.get('source_id') or '?',
                                           (r.get('title') or '')[:60], cid)
            pii.zapisat_nahodku(cfg, 'проверка', gde, leak)
            report.na_podtverzhdenie(gde, len(leak))

    dup = [c for c, n in Counter(r['chunk_id'] for r in rows).items() if n > 1]
    if dup:
        report.problem('дубли chunk_id: %s' % dup[:5])

    docs = Counter(r['source_id'] for r in rows)
    nums = sorted(int(d.split('-')[1]) for d in docs)
    lo, hi = cfg.get('expect_from'), cfg.get('expect_to')
    if nums and lo and hi:
        if nums[0] != int(lo) or nums[-1] != int(hi):
            report.problem('номера %d…%d, ожидалось %s…%s' % (nums[0], nums[-1], lo, hi))
        # Пропуск в своём корпусе — не дыра, если номер занят соседним.
        #
        # Пространство номеров одно на все пять корпусов: DOC-NN уезжает в
        # метаданные фрагмента и в журнал обращений, и два документа под одним
        # номером — молча испорченная база. Поэтому новая инструкция ERP
        # получает не следующий свободный в своём корпусе, а следующий
        # свободный вообще: после DOC-170 это DOC-171.
        #
        # Значит внутри корпуса номера больше не сплошные, и раньше проверка
        # на это ругалась: «пропущенные номера: 57…170». Сто четырнадцать чужих
        # номеров, перечисленных как ошибка, — это ровно тот шум, из-за которого
        # на проверку перестают смотреть.
        #
        # Настоящая дыра осталась дырой: номер, который не занят НИКЕМ.
        from .build import _nomera_sosednih_korpusov
        chuzhie = _nomera_sosednih_korpusov(cfg)
        gaps = [n for n in range(int(lo), int(hi) + 1)
                if n not in nums and n not in chuzhie]
        if gaps:
            report.problem('пропущенные номера: %s' % gaps)
    for d, n in docs.items():
        if n > int(cfg.get('max_chunks_per_doc', 60)):
            report.problem('на документ %s приходится %d фрагментов' % (d, n))

    sizes = sorted(len(r['chunk_text']) for r in rows)
    report.count('фрагментов', len(rows))
    report.count('документов', len(docs))
    report.count('медиана знаков', sizes[len(sizes) // 2] if sizes else 0)
    report.count('максимум знаков', sizes[-1] if sizes else 0)
    report.count('со скриншотами', sum(1 for r in rows if r.get('has_screen')))
    report.count('статусы', dict(Counter(r.get('status') for r in rows)))
    return report


def unknown_terms(cfg, report=None):
    """Сокращения вне известного списка — не ошибка, а повод свериться с
    картинкой. Из семи проверенных «неизвестных» шесть оказались ошибками
    чтения коротких слов, а не новыми терминами."""
    report = report or Report('terms')
    known = known_abbr(cfg)
    common = {'RUB', 'USD', 'EUR', 'ERP', 'CRM', 'НСИ', 'УПД', 'НДС', 'ИНН', 'КПП',
              'ОГРН', 'ГТД', 'ЭДО', 'АРМ', 'ТОРГ', 'ФИО', 'ПО', 'ОС', 'ПК', 'РФ',
              'PDF', 'XML', 'MS', 'ВАЖНО', 'НЕТ', 'ДА', 'ОБ', 'СМ', 'ГГ', 'ШТ', 'КГ',
              'ММ', 'ЕСЛИ', 'ИЛИ', 'ЧТО', 'ДЛЯ', 'ПРИ', 'ВСЕ', 'КАК', 'НА', 'ОТ'}
    found = {}
    rows = [json.loads(l) for l in io.open(cfg['out'], encoding='utf-8') if l.strip()]
    for r in rows:
        for a in set(ABBR.findall(r['chunk_text'])) | set(LATIN.findall(r['chunk_text'])):
            if a not in known and a not in common:
                found.setdefault(a, set()).add(r['source_id'])
    rows_out = sorted(((a, d) for a, d in found.items() if len(d) >= 2),
                      key=lambda x: (-len(x[1]), x[0]))
    path = cfg.get('terms_out')
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write('сокращение;документов;где встретилось\n')
            for a, docs in rows_out:
                fh.write('%s;%d;%s\n' % (a, len(docs), ' '.join(sorted(docs)[:5])))
        report.count('файл', path)
    report.count('сокращений на расшифровку', len(rows_out))
    report.note(', '.join(a for a, _ in rows_out[:15]))
    return report
