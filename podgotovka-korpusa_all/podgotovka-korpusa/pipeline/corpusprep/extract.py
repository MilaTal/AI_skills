# -*- coding: utf-8 -*-
"""Этапы «реестр» и «извлечение».

Реестр — по строке на исходный файл: формат, дата, объём текста, число экранов,
плюс колонки ручной разметки, которые при пересборке не затираются.

Извлечение — текст docx в markdown с плейсхолдерами на местах картинок и сами
картинки в media. Картинка привязана к абзацу, после которого стояла: инструкция
говорит «нажмите кнопку», а следом идёт экран с этой кнопкой, и порядок терять
нельзя.
"""
import io
import os
import re
import shutil
import subprocess
import zipfile
import datetime
import xml.etree.ElementTree as ET

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table

from .util import read_csv, write_csv, slug, Report

RASTR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emf_v_png.ps1')
VEKTOR = ('.emf', '.wmf')


def rasterizovat(media_dir, report=None):
    """Векторные экраны EMF/WMF — в PNG. Возвращает {было: стало} по вышедшим.

    Зачем. Word хранит схемы процессов не картинкой, а метафайлом Windows.
    Распознавание принимает только растр, и такие экраны выпадали молча: в
    отчёте строка «формат не поддерживается», в базе — дырка на месте схемы.

    Нашлось на методике ERP: 127 экранов из 362 оказались метафайлами, и среди
    них ВСЕ схемы процессов — то самое «как устроено», ради чего методику и
    брали. Треть слоя не доехала бы до базы, а прогон при этом считался бы
    удавшимся.

    Поэтому растеризация стоит здесь, в извлечении, а не отдельным ручным
    шагом: место, где экран впервые кладётся на диск, — единственное, мимо
    которого не пройдёт ни один будущий документ.
    """
    vektor = [f for f in os.listdir(media_dir)
              if os.path.splitext(f)[1].lower() in VEKTOR]
    if not vektor:
        return {}
    try:
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', RASTR, '-Papka', media_dir],
                       capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        if report:
            report.problem('растеризация не запустилась: %s' % e)
        return {}
    gotovo = {}
    for f in vektor:
        if os.path.exists(os.path.join(media_dir, f + '.png')):
            gotovo[f] = f + '.png'
    if report and len(gotovo) < len(vektor):
        report.problem('векторных экранов %d, растеризовано %d — папка %s'
                       % (len(vektor), len(gotovo), os.path.basename(media_dir)))
    return gotovo


VML_IMAGEDATA = '{urn:schemas-microsoft-com:vml}imagedata'
R_ID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
NS = {'dcterms': 'http://purl.org/dc/terms/', 'dc': 'http://purl.org/dc/elements/1.1/',
      'cp': 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'}
MANUAL = ['solution', 'doc_type', 'doc_subtype', 'owner', 'comment']
FIELDS = ['file', 'system', 'ext', 'size_kb', 'updated', 'screen_years', 'chars',
          'paragraphs', 'headings', 'images', 'status'] + MANUAL
CONTEXT_LINES = 2
DATE_RE = re.compile(r'\b(\d{2})[.](\d{2})[.](\d{2,4})\b')


def count_screens(path):
    """Экраны в теле документа. Маркеры списков из numbering.xml не считаем —
    это буллеты. Внешние cid-ссылки тоже: у них есть встроенная копия, которая
    считается отдельно, иначе один экран посчитается дважды."""
    with zipfile.ZipFile(path) as z:
        try:
            rels = ET.fromstring(z.read('word/_rels/document.xml.rels'))
        except KeyError:
            return 0, 0
    imgs = [r for r in rels if 'image' in (r.get('Type') or '')]
    inner = sum(1 for r in imgs if r.get('TargetMode') != 'External')
    outer = sum(1 for r in imgs if r.get('TargetMode') == 'External')
    return inner, outer


def word_props(path):
    out = {'created': '', 'modified': '', 'author': '', 'revision': ''}
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read('docProps/core.xml'))
    except Exception:
        return out
    def txt(tag):
        el = root.find(tag, NS)
        return (el.text or '').strip() if el is not None else ''
    out['created'] = txt('dcterms:created')[:10]
    out['modified'] = txt('dcterms:modified')[:10]
    out['author'] = txt('dc:creator')
    out['revision'] = txt('cp:revision')
    return out


def build_registry(cfg, report=None):
    report = report or Report('registry')
    src, system = cfg['source_dir'], cfg['system']
    path = cfg['registry']
    old = {r['file']: r for r in read_csv(path)}
    rows = []
    for f in sorted(os.listdir(src)):
        p = os.path.join(src, f)
        if not os.path.isfile(p) or f.startswith(('.', '~$')):
            continue
        ext = os.path.splitext(f)[1].lower()
        st = os.stat(p)
        updated = datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d')
        r = dict.fromkeys(FIELDS, '')
        r.update(file=f, system=system, ext=ext, size_kb=round(st.st_size / 1024),
                 updated=updated, status='')
        if ext == '.docx':
            try:
                doc = Document(p)
                r['chars'] = len(''.join(x.text for x in doc.paragraphs))
                r['paragraphs'] = sum(1 for x in doc.paragraphs if x.text.strip())
                r['headings'] = sum(1 for x in doc.paragraphs
                                    if x.style.name.lower().startswith(('heading', 'заголовок')))
                inner, outer = count_screens(p)
                r['images'] = inner
                if outer:
                    r['comment'] = ('картинок, вставленных из письма: %d — '
                                    'экраны на месте, встроенной копией' % outer)
            except Exception as e:
                r['status'] = 'не читается: ' + e.__class__.__name__
        elif ext == '.doc':
            r['status'] = 'нужна конвертация в docx'
        elif ext == '.pdf':
            r['status'] = 'проверить на дубль docx'
        for k in MANUAL:                      # ручная разметка не затирается
            if old.get(f, {}).get(k):
                r[k] = old[f][k]
        if not r['solution']:
            r['solution'] = 'в базу' if ext == '.docx' else 'исключён, не docx'
        rows.append(r)
    write_csv(path, rows, FIELDS)
    report.count('файлов', len(rows))
    report.count('в базу', sum(1 for r in rows if r['solution'] == 'в базу'))
    report.count('экранов', sum(int(r['images'] or 0) for r in rows))
    return rows, report


def excluded(cfg):
    return {r['file'] for r in read_csv(cfg['registry'])
            if 'исключ' in (r.get('solution') or '').lower()}


def images_of(par):
    rids = []
    for el in par._p.iter():
        if el.tag == qn('a:blip'):
            rid = el.get(qn('r:embed')) or el.get(qn('r:link'))
        elif el.tag == VML_IMAGEDATA:
            rid = el.get(R_ID)
        else:
            continue
        if rid and rid not in rids:
            rids.append(rid)
    return rids


from . import pii as _pii

# Телефоны и пароли не попадают в базу знаний ни в каком виде: их нет смысла
# искать, а риск от их хранения ненулевой.
EXT_PHONE = re.compile(r'\(?\s*доб\.?\s*\d{1,5}\s*\)?', re.I)
# Только настоящие телефонные форматы: со скобками, с восьмёркой или через
# дефисы. Иначе под замену попадали номера документов вида 000000000019.
BARE_PHONE = re.compile(r'(?<!\d)(?:\(\d{3,5}\)\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}|8[\s-]\d{3}[\s-]\d{3}[\s-]\d{2}[\s-]\d{2}|\d{3}-\d{2}-\d{2})(?!\d)')


MARK = 'ХХХ'          # отметка на месте изъятого: видно, что данные убраны намеренно

# Имена, которые надо вычистить. Список приходит из конфига корпуса
# (pii_names) и ставится один раз на прогон — см. nazvat_imena.
#
# Зачем это добавлено 16.09.2026. До этого дня список имён расширял только
# ОБНАРУЖЕНИЕ: проверка находила ФИО и писала о них в отчёт, а вычистка умела
# убирать лишь телефоны и пароли. Половина работы: детектор видит любое имя,
# убрать не может ни одного.
#
# Поймали на первой сборке методики. На распознанных экранах печатных форм —
# заявок и актов — стояли подписанты, а в планировщике работ исполнитель.
# Проверка их нашла, отчёт их назвал, и дальше они остались бы в базе.
#
# Почему списком, а не по шаблону ФИО. Шаблон ловит и то, что именем не
# является: название документа, имя в примере, «Иванов И.И.» из образца
# заполнения. Вычищать по шаблону значит портить текст там, где имя — часть
# смысла. Список ведёт человек: проверка показала имя, человек решил, что его
# надо убрать, и внёс. Обнаружение остаётся общим и ловит новые.
_IMENA = []


def nazvat_imena(cfg):
    """Взять список имён из конфига корпуса. Вызывается перед разбором."""
    global _IMENA
    _IMENA = sorted(_pii._extra(cfg), key=len, reverse=True)
    return len(_IMENA)


# --- знаки, которые Word отдаёт нечитаемыми ------------------------------------
#
# Маркеры списков Word рисует символьными шрифтами — Symbol и Wingdings. В
# документе это кружок или квадратик, а в тексте приезжает знак из приватной
# области Юникода: у каждого шрифта там свой смысл, и без самого шрифта
# показать его нечем. В любом просмотрщике и в нашей выдаче это «□».
#
# Замерено 23.09.2026 по нарезке корпуса: U+F0B7 — 21 раз, U+F020 — 20 раз,
# все в модели ERP. Документ при этом читается: пункт списка есть, а маркера
# нет — значит подменять его надо обычным дефисом, а не выбрасывать строку.
SIMVOLNYE_MARKERY = {
    '': '- ',   # Symbol: •
    '': '- ',   # Wingdings: ▪
    '': '- ',   # Wingdings: □  — тот самый квадратик из просмотра
    '': '- ',   # Wingdings: ●
    '': '- ',   # Wingdings: ❑
    '': '- ',   # Wingdings: ➢
    '': '- ',   # Wingdings: ✔
    '': ' ',    # Symbol: пробел
}
PRIVATNAYA_OBLAST = re.compile('[-]')
NERAZRYVNYY_PROBEL = ' '
POTERYANNYY_ZNAK = '�'

# Счётчики для отчёта. Молча починенный знак — почти то же, что молча
# испорченный: через месяц никто не вспомнит, откуда в тексте дефисы.
ZNAKI = {'markery': 0, 'privatnye': 0, 'nerazryvnye': 0, 'poteryannye': 0}


def pochinit_znaki(text):
    """Привести знаки к читаемым — и посчитать, сколько чего было.

    U+FFFD НЕ чинится и не выбрасывается. Это не оформление, а потеря: файл
    прочитан не в той кодировке, и один знак уже не восстановить. Замер
    23.09.2026: два таких в DOC-119, «Про?ести» вместо «Провести». Такое
    должно попасть человеку на глаза, а не раствориться в нормализации.
    """
    for znak, zamena in SIMVOLNYE_MARKERY.items():
        if znak in text:
            ZNAKI['markery'] += text.count(znak)
            text = text.replace(znak, zamena)
    ostalos = PRIVATNAYA_OBLAST.findall(text)
    if ostalos:
        ZNAKI['privatnye'] += len(ostalos)
        text = PRIVATNAYA_OBLAST.sub(' ', text)
    if NERAZRYVNYY_PROBEL in text:
        ZNAKI['nerazryvnye'] += text.count(NERAZRYVNYY_PROBEL)
        text = text.replace(NERAZRYVNYY_PROBEL, ' ')
    if POTERYANNYY_ZNAK in text:
        ZNAKI['poteryannye'] += text.count(POTERYANNYY_ZNAK)
    return text


def scrub(text):
    text = pochinit_znaki(text)
    text = _pii.PHONE.sub(MARK, text)
    text = BARE_PHONE.sub(MARK, text)
    text = EXT_PHONE.sub('', text)
    text = _pii.PASSWORD.sub(lambda m: m.group(0).replace(m.group(1), MARK), text)
    # Длинные написания заменяются раньше коротких: иначе «Земскова» съест
    # начало «Земскова Светлана Владимировна» и оставит хвост из имени и
    # отчества — по отдельности они выглядят безобидно, а вместе это те же
    # персональные данные.
    for imya in _IMENA:
        if imya in text:
            text = text.replace(imya, MARK)
    return re.sub(r'\s{2,}', ' ', text).strip()


def as_markdown(par):
    # Добавочный номер указывает на конкретного сотрудника — вычищаем, общий
    # номер компании остаётся: он часть инструкции и полезен в ответе.
    text = scrub(par.text)
    if not text:
        return None
    style = (par.style.name or '').lower()
    if style.startswith(('heading', 'заголовок')):
        m = re.search(r'(\d+)', style)
        return '#' * (min(int(m.group(1)), 4) if m else 2) + ' ' + text
    if text.upper().startswith(('ВАЖНО', 'ВНИМАНИЕ', 'ПРИМЕЧАНИЕ')) and ':' in text[:20]:
        i = text.find(':') + 1
        return '> **' + text[:i] + '** ' + text[i:].strip()
    if 'list' in style or 'список' in style:
        return '- ' + text
    return text


def table_markdown(tbl):
    # Ячейки таблицы чистятся так же, как абзацы: пароль в таблице — такой же
    # пароль, и один раз он именно так и просочился.
    rows = [[scrub(c.text.replace('\n', ' ')) for c in r.cells] for r in tbl.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return None
    w = len(rows[0])
    out = ['| ' + ' | '.join(rows[0]) + ' |', '|' + '---|' * w]
    for r in rows[1:]:
        out.append('| ' + ' | '.join(r[:w]) + ' |')
    return '\n'.join(out)


def join_blocks(lines):
    """Пустая строка между блоками обязательна: по ней нарезка отличает абзац от
    таблицы и от экранной формы. Подряд идущие пункты списка остаются вместе."""
    out, prev_list = [], False
    for line in lines:
        is_list = line.startswith('- ')
        if out and not (is_list and prev_list):
            out.append('')
        out.append(line)
        prev_list = is_list
    return '\n'.join(out)


def extract_one(path, cfg):
    fname = os.path.basename(path)
    doc = Document(path)
    sl = slug(fname)
    system = cfg['system']
    media_rel = 'media/%s/%s' % (system, sl)
    media_dir = os.path.join(cfg['work_dir'], 'media', system, sl)
    os.makedirs(media_dir, exist_ok=True)

    lines, screens, saved, n = [], [], set(), 0
    for child in doc.element.body.iterchildren():
        if child.tag == qn('w:tbl'):
            t = table_markdown(Table(child, doc))
            if t:
                lines.append(t)
            continue
        if child.tag != qn('w:p'):
            continue
        par = Paragraph(child, doc)
        md = as_markdown(par)
        if md:
            lines.append(md)
        for rid in images_of(par):
            part = doc.part.related_parts.get(rid)
            if part is None:                  # внешняя cid-ссылка: копия придёт по r:embed
                continue
            img = os.path.basename(str(part.partname))
            if img not in saved:
                io.open(os.path.join(media_dir, img), 'wb').write(part.blob)
                saved.add(img)
            n += 1
            lines.append('<<ЭКРАН %d: %s/%s>>' % (n, media_rel, img))
            screens.append({'n': n, 'image': '%s/%s' % (media_rel, img),
                            'anchor': len(lines) - 1})

    # Векторные экраны переводим в растр и подменяем ссылки — и в тексте, и в
    # описи экранов. Подменять надо в обоих местах: текст ведёт распознанный
    # разбор на место, а опись ведёт распознавание к файлу.
    zamena = rasterizovat(media_dir)
    if zamena:
        for i, l in enumerate(lines):
            if l.startswith('<<ЭКРАН'):
                for bylo, stalo in zamena.items():
                    if l.endswith('/' + bylo + '>>'):
                        lines[i] = l[:-(len(bylo) + 2)] + stalo + '>>'
                        break
        for scr in screens:
            papka, _, imya = scr['image'].rpartition('/')
            if imya in zamena:
                scr['image'] = papka + '/' + zamena[imya]

    title = next((l.lstrip('# ').strip() for l in lines if l.startswith('#')),
                 next((l for l in lines if l and not l.startswith(('<<', '-', '>', '|'))), sl))
    head = ['---', 'source_file: %s' % fname, 'system: %s' % system,
            'doc_title: %s' % title[:180], 'screens: %d' % n, '---', '']
    clean_dir = os.path.join(cfg['work_dir'], 'clean', system)
    os.makedirs(clean_dir, exist_ok=True)
    out_path = os.path.join(clean_dir, sl + '.md')
    io.open(out_path, 'w', encoding='utf-8', newline='').write(
        '\n'.join(head) + '\n' + join_blocks([l for l in lines if l]) + '\n')

    text_only = [(i, l) for i, l in enumerate(lines) if l and not l.startswith('<<')]
    for s in screens:
        s['context_before'] = [l for i, l in text_only if i < s['anchor']][-CONTEXT_LINES:]
        s['context_after'] = [l for i, l in text_only if i > s['anchor']][:CONTEXT_LINES]
        s.update(source_file=fname, system=system, doc=sl)
        s.pop('anchor')
    return screens, n


def extract(cfg, report=None):
    report = report or Report('extract')
    skip = excluded(cfg)
    src = cfg['source_dir']
    manifest, docs, total = [], 0, 0

    # Исключённый документ убираем из разбора, а не просто перестаём добавлять.
    #
    # Разбор пишется в clean/ один раз и там лежит. Пометишь файл исключённым —
    # извлечение его пропустит, а прежний разбор останется, и нарезка соберёт
    # фрагменты из папки clean, где он по-прежнему есть. Прогон при этом
    # успешен: ни ошибки, ни предупреждения, и в отчёте реестра честно написано
    # «в базу 31». А документов в выходном файле 32.
    #
    # Поймано 16.09.2026 на методике: исключил дубль, пересобрал, получил тот же
    # дубль с тем же номером.
    clean_dir = os.path.join(cfg['work_dir'], 'clean', cfg['system'])
    for f in sorted(skip):
        staryy = os.path.join(clean_dir, os.path.splitext(f)[0] + '.md')
        if os.path.exists(staryy):
            os.remove(staryy)
            report.problem('убран прежний разбор исключённого: %s' % f)

    for f in sorted(os.listdir(src)):
        if not f.lower().endswith('.docx') or f.startswith('~$') or f in skip:
            continue
        try:
            screens, n = extract_one(os.path.join(src, f), cfg)
        except Exception as e:
            report.problem('%s: %s: %s' % (f, e.__class__.__name__, e))
            continue
        manifest.extend(screens)
        docs += 1
        total += n
    man_path = os.path.join(cfg['work_dir'], 'ocr', 'manifest.json')
    os.makedirs(os.path.dirname(man_path), exist_ok=True)
    import json
    io.open(man_path, 'w', encoding='utf-8', newline='').write(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    report.count('документов', docs)
    report.count('экранов', total)

    # Знаки говорят о себе сами. Починенное — счётчиком, потерянное —
    # замечанием: первое оформление, второе порча данных.
    if ZNAKI['markery'] or ZNAKI['privatnye']:
        report.count('маркеров списка из символьных шрифтов', ZNAKI['markery'])
    if ZNAKI['nerazryvnye']:
        report.count('неразрывных пробелов выправлено', ZNAKI['nerazryvnye'])
    if ZNAKI['privatnye']:
        report.problem('знаков символьных шрифтов без известного значения: %d — '
                       'убраны пробелом, посмотрите, не потерялся ли смысл'
                       % ZNAKI['privatnye'])
    if ZNAKI['poteryannye']:
        report.problem('знаков U+FFFD: %d. Это НЕ оформление, а потеря: файл '
                       'прочитан не в той кодировке, знак уже не восстановить. '
                       'Смотреть исходный файл, а не разбор' % ZNAKI['poteryannye'])
    return manifest, report


def screen_years(cfg):
    """Годы, видимые на скриншотах. Единственный надёжный признак возраста
    интерфейса: дата файла врёт — десять файлов корпуса ДО имели одну дату
    массового пересохранения."""
    from collections import Counter
    ocr_dir = cfg['ocr_dir']
    out = {}
    if not os.path.isdir(ocr_dir):
        return out
    for root, _, fs in os.walk(ocr_dir):
        ys = []
        for f in fs:
            if not f.endswith('.md'):
                continue
            for _, _, y in DATE_RE.findall(io.open(os.path.join(root, f), encoding='utf-8').read()):
                y = int(y)
                y = 2000 + y if y < 100 else y
                if 2010 <= y <= 2030:
                    ys.append(y)
        if ys:
            out[os.path.basename(root)] = '%d–%d, чаще %d' % (
                min(ys), max(ys), Counter(ys).most_common(1)[0][0])
    return out
