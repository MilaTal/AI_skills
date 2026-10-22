# -*- coding: utf-8 -*-
"""Этап «распознавание экранов».

Модель по умолчанию — deepseek-v4-flash-vision-exp. Рассуждения выключены
намеренно: с ними модель съедала весь лимит токенов на размышления и возвращала
пустой ответ на плотных экранах (reasoning 8000 из 8000, finish_reason length).
Без них разбор полнее, признаки полей точнее, расход в семь раз меньше.

Три проверки после каждого ответа, каждая появилась после реального случая:
пустой ответ, залипание (километр одного символа при формально валидном ответе)
и неубранные персональные данные.
"""
import base64
import io
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from . import pii
from .util import Report

MEDIA_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}
DEGENERATE = re.compile(r'(.)\1{40,}')

SYSTEM_INTRO = """Ты разбираешь скриншоты интерфейса 1С для базы знаний, по которой
сотрудники ищут ответ на вопрос «что нажать и что заполнить». Задача — перевести
экран в машиночитаемый текст глубиной до каждой кнопки и каждого поля.

Отвечай только разбором по шаблону. Никаких вступлений, пояснений и комментариев
до или после — текст пойдёт в базу знаний как есть.

Названия элементов интерфейса переноси дословно, как написано на экране, в тех же
кавычках. Не переводи, не сокращай, не исправляй опечатки интерфейса и не
пересказывай своими словами: пользователь спрашивает теми словами, которые видит
на кнопке, и пересказ здесь равен потере.

Если элемент не читается — так и пиши: «не читается». Не додумывай названия.

В колонке «Признак на экране» пиши только то, что видно: «подчёркнуто красным»,
«жёлтая заливка», «жёлтая рамка», «серое, недоступно», «без пометки». Не выводи
обязательность поля из смысла — только из вида поля на картинке.

ОБЕЗЛИЧИВАНИЕ, ОБЯЗАТЕЛЬНОЕ. Любое реальное ФИО сотрудника заменяй на
«Иванов И.И.» — в любом виде: полностью, инициалами с фамилией, в строке автора
документа, в полях «Куратор», «Подготовил», «ФИО командированного». Телефоны,
пароли и паспортные данные заменяй на «ХХХ» — тремя русскими буквами Х. Отметка
нужна, чтобы было видно: данные убраны намеренно, а не потеряны. Адреса служебных почтовых ящиков и
названия организаций оставляй как есть — они часть инструкции.

Ниже — обязательный формат ответа.

"""


def out_path(cfg, screen):
    return os.path.join(cfg['ocr_dir'], screen['doc'], '%03d.md' % screen['n'])


def _one(cfg, key, system_prompt, screen, force, stats, lock, report):
    dst = out_path(cfg, screen)
    if os.path.exists(dst) and not force:
        with lock:
            stats['skipped'] += 1
        return

    # ДЕНЕЖНЫЙ ПОТОЛОК. Проверяется перед каждым экраном, а не после прогона.
    #
    # Распознавание — самое дорогое, что у нас есть, и дорогое с большим
    # отрывом: один прогон по 73 экранам журналов стоил 261 703 токена на входе,
    # а весь агент приёмки тратит 899 токенов на обращение. Ставить денежный
    # потолок на агента — значит огораживать копейку, оставив рубль без
    # ограды.
    #
    # Потолок стоит НА ПРОГОН, а не на документ и не на месяц. Так он и работает
    # как тормоз: упёрлись — остановились, разобрались, при необходимости
    # запустили снова. Уже распознанные экраны кешированы, повтор их не
    # оплачивает, поэтому остановка не выбрасывает потраченное.
    # Деньги у поставщика кончились — остальные экраны не начинаем вовсе.
    # Проверка стоит здесь же, где и потолок токенов: оба означают «дальше не
    # тратим», разница только в том, кто поставил предел — мы или поставщик.
    if stats.get('dengi_konchilis'):
        with lock:
            stats['ostanovleno_dengami'] = stats.get('ostanovleno_dengami', 0) + 1
        return

    predel = cfg.get('predel_tokenov_na_progon')
    if predel:
        with lock:
            potracheno = stats['tokens_in'] + stats['tokens_out']
            # Запас на то, что уже в полёте.
            #
            # Проверка стоит ПЕРЕД обращением к модели, а потоков шесть. Значит
            # шестеро могут пройти проверку одновременно, и все шестеро
            # заплатят. Первый замер это и показал: потолок 20 000, потрачено
            # 40 838 — перелёт вдвое.
            #
            # Поэтому останавливаемся заранее: вычитаем из потолка стоимость
            # того, что может быть в полёте, — число потоков на среднюю цену
            # экрана. Средняя считается по уже распознанному в этом прогоне; пока
            # не распознано ничего, берём 4500 — самый дорогой экран из наших
            # замеров, плотная форма 1С.
            #
            # Потолок от этого становится чуть строже обещанного, и это верная
            # сторона ошибки: лучше остановиться раньше, чем заплатить вдвое.
            #
            # ЧЕСТНАЯ ФОРМУЛИРОВКА ГАРАНТИИ. Потолок не жёсткий и жёстким быть не
            # может: проверка стоит до обращения, а обращений идёт несколько
            # разом. Гарантия такая — расход не превысит потолок больше чем на
            # один батч, то есть на число потоков умножить на цену экрана. Нужна
            # граница точнее — уменьшайте число потоков, другого способа нет.
            if stats['done']:
                srednyaya = potracheno / float(stats['done'])
                zapas = int(cfg.get('recognizer', {}).get('workers', 6)) * srednyaya
            else:
                # Ничего ещё не распознано — считать среднюю не по чему, и
                # запрещать старт нельзя: иначе потолок меньше одного батча
                # запрещает работу вовсе. Первый батч проходит всегда.
                zapas = 0
            if potracheno + zapas >= int(predel):
                stats['ostanovleno_predelom'] += 1
                return

    img = os.path.join(cfg['work_dir'], *screen['image'].split('/'))
    media = MEDIA_TYPES.get(os.path.splitext(img)[1].lower())
    if media is None:
        with lock:
            stats['failed'] += 1
        report.problem('формат не поддерживается: %s' % screen['image'])
        return

    before = '\n'.join(screen.get('context_before') or []) or '(нет текста перед экраном)'
    after = '\n'.join(screen.get('context_after') or []) or '(нет текста после экрана)'
    prompt = ('Инструкция: %s\nЭто экран номер %d в документе.\n\n'
              'Текст инструкции перед экраном:\n%s\n\n'
              'Текст инструкции после экрана:\n%s\n\n'
              'Разбери экран по шаблону. Первая строка ответа — '
              '«### ⌸ Экран %d — <заголовок окна>».'
              % (screen['source_file'], screen['n'], before, after, screen['n']))
    data = base64.standard_b64encode(io.open(img, 'rb').read()).decode('utf-8')
    r = cfg['recognizer']
    payload = {
        'model': r['model'], 'max_tokens': r.get('max_tokens', 8000),
        'thinking': {'type': 'disabled'},
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': [
                {'type': 'image_url',
                 'image_url': {'url': 'data:%s;base64,%s' % (media, data),
                               'detail': r.get('detail', 'high')}},
                {'type': 'text', 'text': prompt}]}]}

    text, leak = '', []
    for attempt in range(1, int(r.get('attempts', 3)) + 1):
        try:
            resp = requests.post(r['url'], timeout=int(r.get('timeout', 180)),
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'},
                                 json=payload)
        except requests.RequestException as e:
            report.note('сеть, попытка %d: %s экран %d (%s)'
                        % (attempt, screen['doc'], screen['n'], e.__class__.__name__))
            continue
        if resp.status_code == 401:
            report.problem('ключ не принят')
            with lock:
                stats['failed'] += 1
            return
        # Отказ ПО ДЕНЬГАМ отличается от любой другой ошибки, и обращаться с ним
        # надо иначе.
        #
        # У поставщика распознавания стоит месячный лимит 20 долларов. Пока он не
        # исчерпан, всё работает; когда исчерпан — не поможет ни одна попытка из
        # трёх, и остальные экраны тоже не пройдут. Повторять здесь значит
        # тратить время впустую и заканчивать молчанием: в отчёте осталась бы
        # строка «ошибка 402», из которой человеку непонятно ничего.
        #
        # Поэтому прогон останавливается целиком и сразу, а оператор получает не
        # код ошибки, а то, что ему надо сделать: посмотреть кабинет и решить,
        # платить ли. Это то же правило, что и у всех наших пределов — исход
        # один, к человеку.
        if resp.status_code in (402, 403) or (
                resp.status_code == 429 and 'balance' in resp.text.lower()):
            with lock:
                if not stats.get('dengi_konchilis'):
                    stats['dengi_konchilis'] = True
                    report.problem(
                        'поставщик распознавания отказал по деньгам (код %s). '
                        'Похоже, исчерпан месячный лимит. Прогон остановлен '
                        'целиком: повторять бесполезно, остальные экраны тоже '
                        'не пройдут. Оператору: открыть кабинет, понять причину '
                        'и решить, пополнять ли. Загрузка новых инструкций '
                        'встанет до решения' % resp.status_code)
                stats['failed'] += 1
            return
        if resp.status_code != 200:
            report.note('ошибка %s, попытка %d: %s экран %d'
                        % (resp.status_code, attempt, screen['doc'], screen['n']))
            continue
        body = resp.json()
        text = (body['choices'][0]['message'].get('content') or '').strip()
        usage = body.get('usage') or {}
        with lock:
            stats['tokens_in'] += usage.get('prompt_tokens', 0)
            stats['tokens_out'] += usage.get('completion_tokens', 0)
        if not text:
            report.note('пустой ответ, попытка %d: %s экран %d'
                        % (attempt, screen['doc'], screen['n']))
            continue
        if DEGENERATE.search(text):
            report.note('залипший ответ, попытка %d: %s экран %d'
                        % (attempt, screen['doc'], screen['n']))
            text = ''
            continue
        leak = pii.find(text, cfg)
        if not leak:
            break
        payload['messages'][1]['content'][1]['text'] = (
            prompt + '\n\nВ прошлый раз в ответе остались настоящие ФИО: '
            + ', '.join(leak) + '. Замени каждое на «Иванов И.И.», включая инициалы.')

    if not text:
        with lock:
            stats['failed'] += 1
        report.problem('не распознан: %s экран %d' % (screen['doc'], screen['n']))
        return
    if leak:
        # Находка уходит в очередь к оператору, а не в замечания конвейера: это
        # не поломка разбора, а вопрос к человеку — настоящее имя или ложное
        # срабатывание. И уходит БЕЗ ЗНАЧЕНИЙ: сами значения пишутся в рабочую
        # папку корпуса, наружу идёт только «где и сколько».
        with lock:
            stats['pii'] += 1
            pii.zapisat_nahodku(cfg, 'распознавание',
                                '%s экран %d' % (screen['doc'], screen['n']), leak)
            report.na_podtverzhdenie('%s экран %d' % (screen['doc'], screen['n']),
                                     len(leak))

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    header = ('<!-- источник: %s | картинка: %s | модель: %s -->\n\n'
              % (screen['source_file'], screen['image'], r['model']))
    io.open(dst, 'w', encoding='utf-8', newline='').write(header + text + '\n')
    with lock:
        stats['done'] += 1


def load_key(cfg):
    """Ключ из окружения, иначе из файла, указанного в конфиге. В n8n придёт
    переменной окружения из credentials, локально удобнее файлом."""
    r = cfg['recognizer']
    var = r.get('api_key_env', 'DEEPSEEK_API_KEY')
    if os.environ.get(var):
        return os.environ[var]
    path = r.get('api_key_file') or cfg.get('env_file')
    if path and os.path.exists(path):
        for line in io.open(path, encoding='utf-8-sig'):
            line = line.strip()
            if line.startswith(var + '='):
                return line.partition('=')[2].strip().strip('"').strip("'")
    return ''


def recognize(cfg, manifest=None, force=False, report=None):
    report = report or Report('recognize')
    r = cfg['recognizer']
    if manifest is None:
        man_path = os.path.join(cfg['work_dir'], 'ocr', 'manifest.json')
        manifest = json.load(io.open(man_path, encoding='utf-8'))

    pending = manifest if force else [s for s in manifest if not os.path.exists(out_path(cfg, s))]
    if not pending:
        # Все экраны уже разобраны — ключ не нужен. Так пересборка нарезки
        # ничего не стоит и не требует доступа к модели.
        report.count('done', 0)
        report.count('skipped', len(manifest))
        report.note('все экраны уже распознаны, обращение к модели не требуется')
        return report

    key = load_key(cfg)
    if not key:
        report.problem('нет ключа: %s не задан ни в окружении, ни в файле'
                       % r.get('api_key_env', 'DEEPSEEK_API_KEY'))
        return report

    spec = io.open(cfg['screen_format'], encoding='utf-8').read()
    system_prompt = SYSTEM_INTRO + spec
    stats = {'done': 0, 'skipped': len(manifest) - len(pending), 'failed': 0,
             'pii': 0, 'tokens_in': 0, 'tokens_out': 0, 'ostanovleno_predelom': 0,
             'ostanovleno_dengami': 0, 'dengi_konchilis': False}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=int(r.get('workers', 6))) as pool:
        for s in pending:
            pool.submit(_one, cfg, key, system_prompt, s, force, stats, lock, report)
    for k, v in stats.items():
        if k != 'dengi_konchilis':
            report.count(k, v)

    # Поставщик отказал по деньгам — отдельное замечание, и оно важнее прочих.
    #
    # Разница с нашим потолком токенов тут не в механике, а в том, кто может
    # снять предел. Наш снимается правкой конфига; этот — только оплатой, и
    # решение об оплате принимает человек, а не система.
    #
    # Что при этом происходит с процессом. Загрузка новых инструкций встаёт до
    # решения. Это НЕ авария: пока инструкции нет в базе, сотрудники разово
    # идут к разработчикам — ровно так, как было до всей этой работы. Замер
    # даёт цену такого простоя: закрываемая ботом нагрузка — 383 632 рубля в
    # год, то есть около 1 550 рублей в рабочий день.
    # Для сравнения: весь месячный лимит поставщика — 20 долларов.
    #
    # Отсюда и тон сообщения: это не «всё сломалось», а «посмотрите кабинет,
    # день простоя стоит примерно столько же, сколько месяц работы».
    if stats['ostanovleno_dengami']:
        report.problem(
            'поставщик отказал по деньгам: не начато экранов %d. Загрузка новых '
            'инструкций встанет до решения оператора. Это не авария: пока '
            'инструкции нет в базе, сотрудники разово идут к разработчикам. '
            'День простоя стоит около 1 550 рублей — примерно столько же, '
            'сколько весь месячный лимит поставщика. Уже распознанное '
            'сохранено, повторный прогон продолжит с места остановки'
            % stats['ostanovleno_dengami'])

    # Упёрлись в потолок — это ЗАМЕЧАНИЕ, а не тихий пропуск.
    #
    # Без этой строки прогон выглядел бы удавшимся: часть экранов распознана,
    # часть «пропущена», отчёт зелёный. Ровно тот род дефекта, который мы ловим
    # третью неделю, — успех прогона без нужного результата.
    if stats['ostanovleno_predelom']:
        report.problem(
            'потолок расхода %s токенов достигнут: не распознано экранов %d. '
            'Потрачено %d токенов (%d на входе, %d на выходе). Уже распознанное '
            'сохранено — повторный прогон продолжит с места остановки и за него '
            'не заплатит'
            % (cfg.get('predel_tokenov_na_progon'), stats['ostanovleno_predelom'],
               stats['tokens_in'] + stats['tokens_out'],
               stats['tokens_in'], stats['tokens_out']))
    return report
