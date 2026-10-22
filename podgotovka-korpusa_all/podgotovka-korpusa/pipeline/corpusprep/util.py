# -*- coding: utf-8 -*-
"""Общее для всех этапов: конфиг, пути, разбор markdown, вывод для машины.

Вынесено сюда, потому что раньше эти куски были скопированы в шести скриптах и
разъезжались: регулярка блока экрана в одном месте была с re.M, в другом без, и
из-за этого 77 фрагментов получили неверный признак has_screen.
"""
import csv
import io
import json
import os
import re
import sys

SCREEN_RE = re.compile(r'^###\s*⌸\s*Экран\s*(\d+)', re.M)
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
RULER_RE = re.compile(r'^\s*[/\\*_=~•·—–-]{8,}\s*$')


def load_config(path):
    """Конфиг в YAML или JSON. YAML читается без внешних библиотек — нам нужны
    только плоские ключи, вложенные словари и списки строк."""
    text = io.open(path, encoding='utf-8-sig').read()
    if path.lower().endswith('.json'):
        return json.loads(text)
    cfg = {}
    stack = [(-1, cfg)]
    for raw in text.splitlines():
        line = raw.split('#')[0].rstrip() if not raw.strip().startswith('#') else ''
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(':')
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        value = value.strip().strip('"').strip("'")
        if value == '':
            node = {}
            parent[key.strip()] = node
            stack.append((indent, node))
        else:
            if value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            elif re.fullmatch(r'-?\d+', value):
                value = int(value)
            parent[key.strip()] = value
    return cfg


def resolve(cfg, base):
    """Относительные пути в конфиге считаются от папки самого конфига."""
    def fix(v):
        if isinstance(v, str) and ('/' in v or '\\' in v) and not os.path.isabs(v) \
                and not v.startswith('http'):
            return os.path.normpath(os.path.join(base, v))
        return v
    return {k: (resolve(v, base) if isinstance(v, dict) else fix(v)) for k, v in cfg.items()}


def read_csv(path):
    if not path or not os.path.exists(path):
        return []
    with io.open(path, encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh, delimiter=';'))


def write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=';')
        w.writeheader()
        w.writerows(rows)


def front_matter(text):
    meta, body = {}, text
    if text.startswith('---'):
        head, _, body = text[3:].partition('---')
        for line in head.strip().splitlines():
            k, _, v = line.partition(':')
            meta[k.strip()] = v.strip()
    return meta, body.strip()


def blocks(body, drop_rulers=True):
    """Блоки, разделённые пустой строкой. Декоративные линейки выбрасываются:
    содержания в них нет, а проверку на залипание модели они ломают."""
    out, buf = [], []
    for line in body.splitlines():
        if drop_rulers and RULER_RE.match(line):
            continue
        if line.strip():
            buf.append(line)
        elif buf:
            out.append('\n'.join(buf))
            buf = []
    if buf:
        out.append('\n'.join(buf))
    return out


def kind(b):
    if SCREEN_RE.match(b):
        return 'screen'
    if HEADING_RE.match(b):
        return 'heading'
    if b.lstrip().startswith('|'):
        return 'table'
    if b.lstrip().startswith(('- ', '* ')):
        return 'list'
    if b.lstrip().startswith('>'):
        return 'note'
    if re.match(r'^\s*\d+[.)]\s', b):
        return 'steps'
    return 'text'


def slug(name):
    s = os.path.splitext(name)[0]
    for ch in '<>:"/\\|?*«»':
        s = s.replace(ch, '')
    return re.sub(r'\s+', ' ', s).strip().rstrip('.')


class Report(object):
    """Отчёт этапа. Печатается одним JSON на stdout, чтобы n8n мог ветвиться по
    результату, а не разбирать человеческий текст."""

    def __init__(self, stage):
        self.data = {'stage': stage, 'ok': True, 'counts': {}, 'problems': [],
                     'notes': [], 'na_podtverzhdenie': []}

    def na_podtverzhdenie(self, gde, skolko):
        """Находка персональных данных: документ, место, сколько совпадений.

        Отдельно от problems, и вот почему. Замечание конвейера — это поломка:
        номер не закрепился, экран не распознался, файл не прочёлся. Чинится
        правкой кода или повтором. Найденные ФИО или пароль — не поломка, а
        вопрос к человеку: это настоящее имя, которое надо убрать, или ложное
        срабатывание вроде «Россия Росма...» — обрезанного названия фирмы в
        ячейке таблицы. Машина этого не решит.

        Свалить одно в другое значит потерять оба: операторская находка утонет
        среди технических замечаний, а техническое замечание будет ждать
        человека, который ему не нужен.

        ЗНАЧЕНИЙ ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО. Этот список уезжает в n8n, оттуда в
        таблицу очереди и в телеграм. Написать сюда найденное имя значит
        вынести персональные данные ровно туда, откуда мы их убираем. Список
        говорит ГДЕ, а не ЧТО; сами значения остаются в рабочей папке корпуса.
        """
        self.data['na_podtverzhdenie'].append({'gde': gde, 'skolko': skolko})
        self.data['ok'] = False

    def count(self, key, value):
        self.data['counts'][key] = value

    def problem(self, text):
        self.data['problems'].append(text)
        self.data['ok'] = False

    def note(self, text):
        self.data['notes'].append(text)

    def emit(self, as_json=True, stream=None):
        stream = stream or sys.stdout
        if as_json:
            stream.write(json.dumps(self.data, ensure_ascii=False, indent=1) + '\n')
        else:
            d = self.data
            stream.write('%s: %s\n' % (d['stage'], 'ок' if d['ok'] else 'ОШИБКИ'))
            for k, v in d['counts'].items():
                stream.write('   %s: %s\n' % (k, v))
            for p in d['problems']:
                stream.write('   ! %s\n' % p)
            for n in d['notes']:
                stream.write('   - %s\n' % n)
            for p in d.get('na_podtverzhdenie', []):
                stream.write('   ? на подтверждение оператором: %s — совпадений %s\n'
                             % (p['gde'], p['skolko']))
        return 0 if self.data['ok'] else 1
