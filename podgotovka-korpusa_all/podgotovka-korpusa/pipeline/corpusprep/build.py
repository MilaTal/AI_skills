# -*- coding: utf-8 -*-
"""Этапы «сборка» и «нарезка».

Сборка подставляет распознанные экраны на места плейсхолдеров. Нарезка режет по
структуре документа, а не по числу знаков, и не разрывает то, что рвать нельзя:
экран, таблицу с подписью, абзац с двоеточием вместе со списком, нумерованные
шаги, абзац с отрицанием вместе с продолжением.

Про отрицания стоит помнить довод: порванная таблица видна глазом сразу, а
оборванное «не поддерживает» выглядит как уверенный правильный ответ.
"""
import hashlib
import io
import json
import os
import re

from .extract import scrub
from .util import (read_csv, front_matter, blocks, kind, slug,
                   SCREEN_RE, HEADING_RE, Report)

PLACEHOLDER = re.compile(r'^<<ЭКРАН (\d+): (.+?)>>$', re.M)
FIELDS = ['chunk_id', 'chunk_text', 'source_id', 'title', 'document_type', 'topic',
          'version', 'status', 'source_url', 'updated_at', 'backup_url', 'section',
          'doc_subtype', 'system', 'source_file', 'screen_years', 'has_screen',
          'block_id', 'block_title', 'ticket', 'doc_date', 'stage']

# Блок — заголовок первого уровня. Журналы доработок ERP устроены так: вводная
# строка, дальше блоки вида «26-ОТО413. Тема – Этап 3/3». Номер обращения есть у
# трёх блоков из четырёх; у остальных блок получает порядковый номер. Заголовки
# второго уровня и глубже — структура внутри блока, отдельными единицами не
# становятся.
TICKET_RE = re.compile(r'(\d{2})\s*[-–]\s*([А-ЯA-Z]{2,6})\s*(\d{2,4})')
STAGE_RE = re.compile(r'[–—-]\s*(?:\d+\s*)?[Ээ]тап\s*[–—-]?\s*(\d+\s*/\s*\d+)')
# Две записи даты в именах наших файлов: «ERP 03.06.2026» и «ЗНО_2025.02.24».
# Порядок разбора важен: сначала год впереди, иначе «2025.02.24» прочитается как
# 25 февраля 2024 года.
DATE_YMD_RE = re.compile(r'(20\d{2})[.\s_-](\d{2})[.\s_-](\d{2})')
DATE_DMY_RE = re.compile(r'(\d{2})[.\s_-](\d{2})[.\s_-](20\d{2}|\d{2})(?!\d)')


def parse_block_head(text):
    """Заголовок блока → номер обращения, тема, этап.

    Ничего не додумывается: нет номера — пусто, нет этапа — пусто. В одном
    заголовке встречаются два номера подряд («25-ОТО15, 25-ОТО20. Тема»); первый
    берётся как номер блока, остальные остаются в теме, чтобы не потерять связь.
    """
    stage = ''
    m = STAGE_RE.search(text)
    if m:
        stage = m.group(1).replace(' ', '')
        text = STAGE_RE.sub('', text)
    ticket = ''
    t = TICKET_RE.search(text)
    if t:
        ticket = '%s-%s%s' % (t.group(1), t.group(2), t.group(3))
        text = text[:t.start()] + text[t.end():]
    return ticket, text.strip(' .,;:–—-') or '', stage


def date_from_name(name):
    """Дата документа из имени файла: «Обновление рабочей базы ERP 03.06.2026».

    Почему имя, а не дата файла в системе. Дату в имени ставит автор документа, и
    она означает, когда доработка ушла в базу. Дату файла ставит файловая
    система, и её меняет простое копирование: первый же прогон по скопированной
    папке выдал всем десяти журналам сегодняшнее число, и цепочки этапов
    перестали собираться — все редакции оказались одного дня.
    """
    for rx, poryadok in ((DATE_YMD_RE, 'ymd'), (DATE_DMY_RE, 'dmy')):
        m = rx.search(name)
        if not m:
            continue
        if poryadok == 'ymd':
            year, mon, d = m.groups()
        else:
            d, mon, year = m.groups()
            if len(year) == 2:
                year = '20' + year
        try:
            if 1 <= int(mon) <= 12 and 1 <= int(d) <= 31:
                return '%s-%s-%s' % (year, mon, d)
        except ValueError:
            pass
    return ''


def scrub_block(text):
    """Экраны чистятся тем же правилом, что и текст: пароль на скриншоте — такой
    же пароль. Построчно, чтобы не склеить строки таблицы."""
    lines = text.split(chr(10))
    return chr(10).join(scrub(line) if line.strip() else line for line in lines)


def assemble(cfg, report=None):
    report = report or Report('assemble')
    clean_dir = os.path.join(cfg['work_dir'], 'clean', cfg['system'])
    filled = missing = 0
    for f in sorted(os.listdir(clean_dir)):
        if not f.endswith('.md'):
            continue
        path = os.path.join(clean_dir, f)
        text = io.open(path, encoding='utf-8').read()
        doc = os.path.splitext(f)[0]

        def repl(m):
            nonlocal filled, missing
            n = int(m.group(1))
            p = os.path.join(cfg['ocr_dir'], doc, '%03d.md' % n)
            if not os.path.exists(p):
                missing += 1
                report.problem('нет разбора: %s экран %d' % (doc, n))
                return m.group(0)
            block = io.open(p, encoding='utf-8').read()
            if block.lstrip().startswith('<!--'):
                block = block.split('-->', 1)[-1]
            filled += 1
            return '\n' + scrub_block(block.strip()) + '\n'

        new = re.sub(r'\n{3,}', '\n\n', PLACEHOLDER.sub(repl, text))
        if new != text:
            io.open(path, 'w', encoding='utf-8', newline='').write(new)
    report.count('подставлено экранов', filled)
    report.count('не найдено разборов', missing)
    return report


def split_table(block, head, budget):
    """Длинная таблица режется по строкам, шапка повторяется в продолжении.
    Разрыв между строками безопасен, внутри строки и в отрыве от шапки — нет."""
    lines = block.split('\n')
    has_head = len(lines) > 2 and set(lines[1].replace('|', '').strip()) <= set('-: ')
    header = lines[:2] if has_head else []
    parts, cur = [], list(header)
    for row in lines[len(header):]:
        if sum(len(x) + 1 for x in cur + [row]) > budget and len(cur) > len(header):
            parts.append('\n'.join(cur))
            cur = list(header) + [row]
        else:
            cur.append(row)
    if len(cur) > len(header):
        parts.append('\n'.join(cur))
    return [(head + ' — продолжение\n\n' + p if i else p) for i, p in enumerate(parts)]


def split_long(para, head, budget):
    """Границы перебираются по убыванию смысловой силы, и каждая часть, которая
    всё ещё не влезла, режется следующей — до запятой включительно. Одной границы
    мало: перечень кодов операций на 11 тысяч знаков делится только по запятым."""
    if para.lstrip().startswith('|'):
        return split_table(para, head, budget)
    lines = para.split('\n')
    if len(lines) > 2:
        label, rest = lines[0], '\n'.join(lines[1:])
        return [(label + '\n' + p if i == 0 else p)
                for i, p in enumerate(split_table(rest, head, budget))]
    parts = [para]
    for sep in (r'(?<=»),\s+(?=«)', r';\s+', r'(?<=[.!?])\s+', r',\s+'):
        if all(len(p) <= budget for p in parts):
            break
        out = []
        for p in parts:
            if len(p) <= budget:
                out.append(p)
                continue
            items, cur = re.split(sep, p), []
            if len(items) < 2:
                out.append(p)
                continue
            for it in items:
                if sum(len(x) + 2 for x in cur + [it]) > budget and cur:
                    out.append(' '.join(cur))
                    cur = [it]
                else:
                    cur.append(it)
            if cur:
                out.append(' '.join(cur))
        parts = out
    if len(parts) == 1:
        return parts
    return [(head + ' — продолжение\n\n' + p if i else p) for i, p in enumerate(parts)]


def split_screen(text, hard_max):
    head = text.split('\n', 1)[0]
    parts, cur = [], []
    for para in text.split('\n\n'):
        if len(para) > hard_max:
            if cur:
                parts.append('\n\n'.join(cur))
                cur = []
            parts.extend(split_long(para, head, hard_max - len(head) - 120))
            continue
        candidate = cur + [para]
        if sum(len(x) + 2 for x in candidate) > hard_max and cur:
            parts.append('\n\n'.join(cur))
            cur = [head + ' — продолжение', para] if not para.startswith('###') else [para]
        else:
            cur = candidate
    if cur:
        parts.append('\n\n'.join(cur))
    return parts


def units_of(bs):
    units, i, n = [], 0, len(bs)
    while i < n:
        b, k = bs[i], kind(bs[i])
        if k == 'screen':
            parts = [b]
            i += 1
            while i < n and kind(bs[i]) not in ('screen', 'heading'):
                parts.append(bs[i])
                i += 1
            units.append(('\n\n'.join(parts), 'screen'))
            continue
        if k == 'text':
            merged = [b]
            i += 1
            s = b.strip()
            if i < n and (s.endswith(':') or len(s) <= 90) and kind(bs[i]) in ('list', 'table', 'steps'):
                merged.append(bs[i])
                i += 1
            units.append(('\n\n'.join(merged), 'text' if len(merged) == 1 else 'bound'))
            continue
        units.append((b, 'bound' if k in ('table', 'list', 'steps') else 'text'))
        i += 1
    return units


def pack(units, target, hard_max, min_chars):
    chunks, cur = [], []
    for text, u in units:
        piece = text.strip()
        if not piece:
            continue
        if len(piece) > hard_max:
            if cur:
                chunks.append('\n\n'.join(cur))
                cur = []
            chunks.extend(split_screen(piece, hard_max) if u == 'screen'
                          else split_long(piece, piece.splitlines()[0][:60], hard_max - 80))
            continue
        if sum(len(x) + 2 for x in cur + [piece]) > target and cur:
            if sum(len(x) + 2 for x in cur + [piece]) <= hard_max and u != 'text':
                cur.append(piece)
                continue
            chunks.append('\n\n'.join(cur))
            cur = [piece]
        else:
            cur.append(piece)
    if cur:
        chunks.append('\n\n'.join(cur))
    return [c.strip() for c in chunks if len(c.strip()) >= min_chars]


def aktualnost(rows, report):
    """Номер обращения связывает близкие области системы, а не отменяет прежние.

    Так было не всегда, и прежнее правило было неверным. Оно помечало устаревшим
    всё, кроме последней даты: считалось, что доработка приходит редакциями и
    поздняя отменяет раннюю.

    Владелец процесса поправил 14.09.2026: под одним номером в разное время
    выполняется несколько разных доработок. Номер означает, что решается одна
    конкретная задача по системе, — то есть связывает близкие по смыслу области,
    а не выстраивает старшинство.

    Данные это подтвердили сразу. У 25-ОТО757 три блока: «1 Этап. Организация
    структуры», «2 Этап. Отражение данных», «3 Этап. Доработка функционала».
    Прежнее правило помечало первые два устаревшими — а они выполнены и
    действуют. Система прятала работающий порядок, и заметить это по данным было
    нельзя: заголовки читал человек.

    Что осталось. Пометка «устарел» ставится только там, где её поставил человек —
    словом «Неактуально» в имени файла. Выведенной из дат больше нет: выводить
    отмену из того, что документ старше, мы не умеем, и притворяться, что умеем,
    дороже, чем признать.

    Что с этим делать дальше. Связь по номеру остаётся в метаданных и годится для
    поиска: найдя одну часть задачи, можно показать соседние. Отмена же требует
    признака, которого в тексте пока нет, — и пока его нет, все части считаются
    действующими.
    """
    po_tiketu = {}
    for row in rows:
        if row['ticket'] and row['doc_date']:
            po_tiketu.setdefault(row['ticket'], set()).add(row['doc_date'])
    svyazannyh = sum(1 for d in po_tiketu.values() if len(d) > 1)
    report.count('обращений', len(po_tiketu))
    report.count('обращений в нескольких документах', svyazannyh)
    report.count('помечено устаревшими по имени файла',
                 sum(1 for r in rows if r['status'] == 'устарел'))

    # Расхождение «этап против дат» не исправляем молча: автор мог ошибиться в
    # пометке, а мог прийти внеочередной правкой. Разбирает человек.
    for tiket, daty in po_tiketu.items():
        etapy = {}
        for row in rows:
            if row['ticket'] == tiket and row['stage']:
                etapy.setdefault(row['doc_date'], set()).add(row['stage'])
        if len(etapy) < 2:
            continue
        po_date = [sorted(etapy[d])[0] for d in sorted(etapy)]
        nomera = []
        for e in po_date:
            try:
                nomera.append(int(e.split('/')[0]))
            except (ValueError, IndexError):
                nomera.append(0)
        if nomera != sorted(nomera):
            report.problem('обращение %s: этапы не совпадают с порядком дат — %s'
                           % (tiket, ', '.join(po_date)))


def _nomera_sosednih_korpusov(cfg):
    """Номера, выданные в других корпусах. Возвращает множество чисел.

    Ищет таблицы по соседству со своей: work/<корпус>/meta/docids.csv. Свой
    корпус из поиска не исключается — его номера и так в zanyato, повтор
    безвреден.

    Не нашлось ничего — возвращаем пустое множество и работаем как раньше.
    Падать тут нельзя: конвейер должен собираться и на одиноком корпусе.
    """
    put = cfg.get('docids')
    if not put:
        return set()
    # work/<корпус>/meta/docids.csv → work/
    koren_rabot = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(put))))
    if not os.path.isdir(koren_rabot):
        return set()
    nomera = set()
    for papka in sorted(os.listdir(koren_rabot)):
        sosed = os.path.join(koren_rabot, papka, 'meta', 'docids.csv')
        if not os.path.isfile(sosed):
            continue
        for r in read_csv(sosed):
            hvost = str(r.get('source_id') or '').rsplit('-', 1)[-1]
            if hvost.isdigit():
                nomera.add(int(hvost))
    return nomera


def build_chunks(cfg, report=None):
    report = report or Report('chunks')
    reg = {r['file']: r for r in read_csv(cfg.get('registry'))}
    topics = {r['file']: r.get('topic', '') for r in read_csv(cfg.get('topics'))}
    docids = read_csv(cfg.get('docids'))
    fixed = {r['source_file']: r['source_id'] for r in docids}
    titles = {r['source_file']: r.get('title', '') for r in docids}

    # Сколько номеров пришло из таблицы — в отчёт, отдельной строкой.
    #
    # Без этой строки пустая или ненайденная таблица не видна ничем. read_csv
    # молча возвращает пустой список на несуществующем пути, номера тихо
    # раздаёт счётчик, и прогон выглядит удавшимся. Ровно так корпус инструкций
    # ERP прожил с пустым ключом `docids:` до 21.09.2026: двадцать семь номеров
    # переназначались на каждой пересборке, и добавление одного файла сдвинуло бы
    # DOC-55 и DOC-56 — те, что лежат в метаданных рабочей базы.
    #
    # Отдельно отмечаем настроенную, но не найденную таблицу: это не то же
    # самое, что «таблицы не предусмотрено».
    put_docids = cfg.get('docids')
    if put_docids and not os.path.exists(put_docids):
        report.problem('таблица номеров настроена, но не найдена: %s' % put_docids)
    report.count('номеров из таблицы', len(fixed))
    years = cfg.get('_screen_years', {})

    ch = cfg.get('chunk', {})
    target = int(ch.get('target', 1200))
    hard_max = int(ch.get('hard_max', 2500))
    min_chars = int(ch.get('min', 80))

    clean_dir = os.path.join(cfg['work_dir'], 'clean', cfg['system'])
    files = sorted(f for f in os.listdir(clean_dir) if f.endswith('.md'))
    src_of, order = {}, {}
    for f in files:
        meta, _ = front_matter(io.open(os.path.join(clean_dir, f), encoding='utf-8').read())
        src_of[f] = meta.get('source_file', f)
    for f in sorted(files, key=lambda x: src_of[x]):
        if src_of[f] in fixed:
            order[f] = fixed[src_of[f]]

    # Новый файл получает следующий свободный номер, а не место в алфавите.
    # Раньше отсчёт для новых начинался от start_doc независимо от того, что уже
    # закреплено, — и второй прогон выдавал DOC-57 второму документу. Десять
    # номеров разошлись по два раза; таблица номеров такое закреплять
    # отказывается, а сверка без записи в таблице считает файл новым вечно.
    # Так один документ принимался каждые две минуты, а очередь стояла.
    zanyato = set()
    for sid in fixed.values():
        hvost = str(sid).rsplit('-', 1)[-1]
        if hvost.isdigit():
            zanyato.add(int(hvost))

    # Занятыми считаются номера ВСЕХ корпусов, а не только своего.
    #
    # Корпусов у нас пять, у каждого своя таблица номеров и свой start_doc, а
    # пространство номеров ОДНО: DOC-NN уезжает в метаданные фрагмента и в
    # журнал обращений, и два разных документа под одним номером — это молча
    # испорченная база.
    #
    # Поймано 21.09.2026, до того как случилось. Инструкции ERP занимают
    # DOC-30 … DOC-56, и новая инструкция получила бы следующий свободный В
    # СВОЁМ корпусе — DOC-57. А DOC-57 уже принадлежит журналу доработок за
    # 01.09.2026. Свой корпус об этом не знает ничего.
    #
    # Таблицы других корпусов ищутся рядом: work/<корпус>/meta/docids.csv.
    # Читаются только имена номеров — привязка файла к номеру остаётся делом
    # своей таблицы.
    zanyato |= _nomera_sosednih_korpusov(cfg)

    nxt = max([int(cfg.get('start_doc', 1))] + [n + 1 for n in zanyato])
    for f in sorted(files, key=lambda x: src_of[x]):
        if f not in order:
            while nxt in zanyato:
                nxt += 1
            order[f] = 'DOC-%02d' % nxt
            zanyato.add(nxt)
            nxt += 1

    rows, per_doc = [], {}
    for f in files:
        meta, body = front_matter(io.open(os.path.join(clean_dir, f), encoding='utf-8').read())
        src = meta.get('source_file', f)
        r = reg.get(src, {})
        document = os.path.splitext(f)[0]
        # Название документа, а не заголовок внутри него: по названию поиск
        # определяет, о каком документе спрашивает человек, ещё до модели.
        title = titles.get(src) or document
        status = 'устарел' if 'неактуально' in src.lower() else 'актуален'
        idx = 0

        # Единица разбора — блок (заголовок первого уровня). Внутри блока
        # заголовки любого уровня по-прежнему задают раздел: они нужны строке
        # «раздел» и не должны склеиваться между собой при нарезке.
        doc_date = date_from_name(src) or r.get('updated', '')
        head, sections, acc = '', [], []
        # Документ без заголовков первого уровня — это один блок, а не «нулевой».
        # Нулевой появляется только там, где блоки есть: это вводная строка
        # журнала обновлений, стоящая до первого из них.
        est_bloki = any(len(m.group(1)) == 1 for m in
                        (HEADING_RE.match(b) for b in blocks(body)) if m)
        blok_n, blok_head = (0 if est_bloki else 1), ''
        for b in blocks(body):
            m = HEADING_RE.match(b) if kind(b) == 'heading' else None
            if m:
                sections.append((blok_n, blok_head, head, acc))
                head = m.group(2).strip()
                if len(m.group(1)) == 1:
                    blok_n += 1
                    blok_head = head
                acc = []
            else:
                acc.append(b)
        sections.append((blok_n, blok_head, head, acc))

        # Номер фрагмента считается внутри блока, а не сквозным по документу.
        # Иначе добавленный в середину блок сдвигает номера всех последующих
        # фрагментов, и удаление по списку идентификаторов промахивается мимо
        # половины документа.
        v_bloke = {}
        for blok_n, blok_head, head, bs in sections:
            if not bs:
                continue
            ticket, blok_tema, stage = parse_block_head(blok_head) if blok_head else ('', '', '')
            # Блок ноль — то, что стоит до первого заголовка: вводная строка
            # журнала обновлений. Это часть документа, а не отдельная тема.
            block_id = ticket or ('b%02d' % blok_n)
            section = ('%s → %s' % (document, head)) if head else document
            for text in pack(units_of(bs), target, hard_max, min_chars):
                v_bloke[block_id] = v_bloke.get(block_id, 0)
                rows.append({
                    'chunk_id': hashlib.sha1(
                        ('%s#%s#%d' % (src, block_id, v_bloke[block_id])).encode()
                    ).hexdigest()[:16],
                    'block_id': block_id,
                    'block_title': blok_tema or title,
                    'ticket': ticket,
                    'doc_date': doc_date,
                    'stage': stage,
                    'chunk_text': text,
                    'source_id': order[f],
                    'title': title,
                    'document_type': cfg.get('document_type', 'Инструкция'),
                    # Тема документа. У журналов доработок своего файла тем нет
                    # и быть не может: каждый несёт три-четыре разные темы. Для
                    # них в конфиге задаётся одна общая, а тонкая тема живёт в
                    # заголовке блока — block_title.
                    'topic': (topics.get(src) or r.get('doc_type', '')
                              or cfg.get('topic_default', '')),
                    'version': r.get('updated', ''),
                    'status': status,
                    'source_url': cfg.get('url', ''),
                    'updated_at': r.get('updated', ''),
                    'backup_url': cfg.get('url', ''),
                    'section': section,
                    'doc_subtype': r.get('doc_subtype', ''),
                    'system': cfg['system'],
                    'source_file': src,
                    'screen_years': years.get(slug(src), r.get('screen_years', '')),
                    'has_screen': bool(SCREEN_RE.search(text)),
                })
                v_bloke[block_id] += 1
                idx += 1
        per_doc[order[f]] = idx

    aktualnost(rows, report)

    out = cfg['out']
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with io.open(out, 'w', encoding='utf-8', newline='\n') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')

    sizes = sorted(len(r['chunk_text']) for r in rows)
    ids = sorted({r['source_id'] for r in rows}, key=lambda x: int(x.split('-')[1]))
    report.count('документов', len(files))
    report.count('фрагментов', len(rows))
    report.count('медиана знаков', sizes[len(sizes) // 2] if sizes else 0)
    report.count('максимум знаков', sizes[-1] if sizes else 0)
    report.count('длиннее предела', sum(1 for s in sizes if s > hard_max))
    report.count('со скриншотами', sum(1 for r in rows if r['has_screen']))
    report.count('номера', '%s … %s' % (ids[0], ids[-1]) if ids else '')
    report.count('файл', out)
    return rows, report
