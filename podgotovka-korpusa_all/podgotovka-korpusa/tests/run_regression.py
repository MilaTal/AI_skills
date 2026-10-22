# -*- coding: utf-8 -*-
"""Регрессионный тест конвейера.

Гоняет полную цепочку без обращения к модели: два реальных документа корпуса и
их готовые разборы экранов лежат в tests/fixtures. Результат сравнивается с
эталоном tests/fixtures/expected_chunks.jsonl построчно.

Зачем нужен именно такой тест. Правки нарезки и вычистки выглядят безобидно, а
ломают тихо: за время работы мы дважды получили порчу текста — «пароль
закрытого ключа» превратился в «пароль ХХХ ключа», а номер документа
000000000019 был принят за телефон. Оба раза это заметили только глазами.

Запуск:
  python tests/run_regression.py            сравнить с эталоном
  python tests/run_regression.py --update   перезаписать эталон осознанно
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
sys.stdout.reconfigure(encoding='utf-8')

from corpusprep import build, checks, extract   # noqa: E402

FIX = os.path.join(HERE, 'fixtures')
EXPECTED = os.path.join(FIX, 'expected_chunks.jsonl')


def proverit_etalony():
    """Сказать прямо, что эталонов нет, вместо следа ошибки на пол-экрана.

    В переносимом пакете `fixtures` пуст намеренно: там лежали настоящие
    документы компании, часть со скриншотами, где напечатаны ФИО сотрудников.
    Без этой проверки первое, что видит человек на новой машине, —
    FileNotFoundError из середины разбора docx, и причина не названа нигде.
    """
    if os.path.isdir(os.path.join(FIX, 'source')):
        return
    print("Эталонных документов нет — регрессию запускать не на чем.\n"
          "\n"
          "Это не поломка: в переносимом пакете tests/fixtures пуст намеренно.\n"
          "Там лежали настоящие документы компании, и класть их в инструмент,\n"
          "который переезжает на другие машины, нельзя.\n"
          "\n"
          "Как завести свои — в «ЧИТАТЬ ПЕРВЫМ», раздел «Чего в пакете\n"
          "намеренно НЕТ». Коротко: пять своих документов в fixtures/source,\n"
          "один прогон конвейером, проверка глазами — и результат рядом\n"
          "эталоном через `python tests/run_regression.py --update`.")
    sys.exit(2)


def make_cfg(work, out):
    return {
        'system': 'DO',
        'document_type': 'Инструкция',
        'source_dir': os.path.join(FIX, 'source'),
        'work_dir': work,
        'ocr_dir': os.path.join(FIX, 'ocr'),
        'registry': os.path.join(FIX, 'meta', 'registry.csv'),
        'docids': os.path.join(FIX, 'meta', 'docids.csv'),
        'topics': '',
        'abbreviations': os.path.join(FIX, 'meta', 'abbreviations.txt'),
        'screen_format': os.path.join(FIX, 'meta', 'screen-format.md'),
        'out': out,
        'url': 'https://example.test/folder',
        'start_doc': 90,
        'chunk': {'target': 1200, 'hard_max': 2500, 'min': 80},
    }


def run():
    work = tempfile.mkdtemp(prefix='corpusprep-test-')
    out = os.path.join(work, 'chunks.jsonl')
    try:
        cfg = make_cfg(work, out)
        # реестр не пересобираем: в наборе он зафиксирован вместе с разметкой
        extract.extract(cfg)
        cfg['_screen_years'] = extract.screen_years(cfg)
        build.assemble(cfg)
        rows, _ = build.build_chunks(cfg)
        report = checks.validate(cfg, out)
        got = io.open(out, encoding='utf-8').read()
        return rows, report, got
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    proverit_etalony()
    update = '--update' in sys.argv
    rows, report, got = run()

    if update or not os.path.exists(EXPECTED):
        io.open(EXPECTED, 'w', encoding='utf-8', newline='\n').write(got)
        print('эталон записан: %s, фрагментов %d' % (EXPECTED, len(rows)))
        return 0

    want = io.open(EXPECTED, encoding='utf-8').read()
    if got == want:
        print('совпадает с эталоном: фрагментов %d, проверка %s'
              % (len(rows), 'пройдена' if report.data['ok'] else 'НЕ пройдена'))
        return 0 if report.data['ok'] else 1

    a = [json.loads(l) for l in want.splitlines() if l.strip()]
    b = [json.loads(l) for l in got.splitlines() if l.strip()]
    print('РАСХОЖДЕНИЕ С ЭТАЛОНОМ')
    print('   фрагментов было %d, стало %d' % (len(a), len(b)))
    ids_a = {r['chunk_id'] for r in a}
    ids_b = {r['chunk_id'] for r in b}
    if ids_a - ids_b:
        print('   пропали:', sorted(ids_a - ids_b)[:5])
    if ids_b - ids_a:
        print('   появились:', sorted(ids_b - ids_a)[:5])
    common = {r['chunk_id']: r for r in a}
    for r in b:
        old = common.get(r['chunk_id'])
        if old and old != r:
            diff = [k for k in r if old.get(k) != r.get(k)]
            print('   %s изменились поля: %s' % (r['chunk_id'], diff))
            for k in diff:
                if k == 'chunk_text':
                    print('      было:  %s' % str(old[k])[:120].replace('\n', ' '))
                    print('      стало: %s' % str(r[k])[:120].replace('\n', ' '))
                else:
                    print('      %s: %r -> %r' % (k, old.get(k), r.get(k)))
            break
    return 1


if __name__ == '__main__':
    sys.exit(main())
