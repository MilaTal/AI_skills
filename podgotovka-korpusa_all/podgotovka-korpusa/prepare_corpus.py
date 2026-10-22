# -*- coding: utf-8 -*-
"""Подготовка корпуса документов к загрузке в векторную базу — одна точка входа.

Шесть этапов: реестр, извлечение, распознавание экранов, сборка, нарезка,
проверка. Все настройки — в конфиге, путей в коде нет.

Запуск целиком:
  python prepare_corpus.py --config config/do.yaml

Отдельный этап (например, пересобрать только нарезку):
  python prepare_corpus.py --config config/do.yaml --stage chunks

Для вызова из n8n узлом Execute Command:
  python prepare_corpus.py --config config/do.yaml --json

Коды возврата — по ним ветвится флоу:
  0  всё прошло, файл собран и проверен
  1  собран, но проверка нашла замечания
  2  ошибка входа: нет конфига, папки или ключа
  3  сбой распознавания: не удалось получить разбор экранов
"""
import argparse
import io
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pipeline'))
sys.stdout.reconfigure(encoding='utf-8')

from corpusprep import build, checks, extract, zhanr
from corpusprep.util import load_config, resolve, Report

STAGES = ('registry', 'extract', 'recognize', 'assemble', 'chunks', 'validate', 'terms')


def prepare(cfg, stages, force=False):
    """Возвращает список отчётов по этапам. Каждый этап пишет только свою часть,
    чтобы при падении было видно, где именно."""
    reports = []
    os.makedirs(cfg['work_dir'], exist_ok=True)

    if 'registry' in stages:
        _, r = extract.build_registry(cfg)
        reports.append(r)

    manifest = None
    if 'extract' in stages:
        manifest, r = extract.extract(cfg)
        reports.append(r)

    if 'recognize' in stages:
        # импорт внутри этапа: requests нужен только здесь, и без ключа
        # остальные этапы должны работать
        from corpusprep import recognize as rec
        r = rec.recognize(cfg, manifest, force=force)
        reports.append(r)
        if not r.data['ok'] and r.data['counts'].get('done', 0) == 0:
            return reports, 3

    # Годы с экранов считаются после распознавания и до нарезки: это
    # единственный надёжный признак возраста интерфейса.
    # Имена для вычистки берутся из конфига корпуса до любого разбора:
    # ниже они убираются и из текста, и из распознанного экрана, и из
    # ячейки таблицы — тремя разными путями, но одним списком.
    extract.nazvat_imena(cfg)
    cfg['_screen_years'] = extract.screen_years(cfg)

    if 'assemble' in stages:
        reports.append(build.assemble(cfg))

    if 'chunks' in stages:
        _, r = build.build_chunks(cfg)
        reports.append(r)

    if 'validate' in stages:
        reports.append(checks.validate(cfg))

    if 'terms' in stages and cfg.get('terms_out'):
        reports.append(checks.unknown_terms(cfg))

    code = 0 if all(r.data['ok'] for r in reports) else 1
    return reports, code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--stage', default='all', help='all или один из: ' + ', '.join(STAGES))
    ap.add_argument('--from-stage', help='выполнить с этого этапа и до конца')
    ap.add_argument('--force', action='store_true', help='перераспознать готовые экраны')
    ap.add_argument('--json', action='store_true', help='отчёт одним JSON — для n8n')
    ap.add_argument('--set', action='append', default=[],
                    help='переопределить ключ конфига: --set url=https://...')
    a = ap.parse_args()

    if not os.path.exists(a.config):
        print(json.dumps({'ok': False, 'error': 'нет конфига: ' + a.config}, ensure_ascii=False))
        return 2
    cfg = resolve(load_config(a.config), os.path.dirname(os.path.abspath(a.config)))
    for kv in a.set:
        k, _, v = kv.partition('=')
        cfg[k.strip()] = v.strip()

    for key in ('system', 'source_dir', 'work_dir', 'out'):
        if not cfg.get(key):
            print(json.dumps({'ok': False, 'error': 'в конфиге не задан ' + key}, ensure_ascii=False))
            return 2
    if not os.path.isdir(cfg['source_dir']):
        print(json.dumps({'ok': False, 'error': 'нет папки источников: ' + cfg['source_dir']},
                         ensure_ascii=False))
        return 2
    cfg.setdefault('ocr_dir', os.path.join(cfg['work_dir'], 'ocr_result'))

    if a.from_stage:
        stages = STAGES[STAGES.index(a.from_stage):]
    elif a.stage == 'all':
        stages = STAGES
    else:
        stages = (a.stage,)

    try:
        reports, code = prepare(cfg, stages, force=a.force)
    except Exception as e:
        payload = {'ok': False, 'error': '%s: %s' % (e.__class__.__name__, e),
                   'trace': traceback.format_exc().splitlines()[-3:]}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 2

    if a.json:
        # Жанр уезжает в отчёт отдельным полем, а не прячется в конфиге.
        #
        # Решение владельца 20.09.2026: в базу идут только инструкции ERP и ДО,
        # остальное — отказ в переносе с сообщением оператору. Отказывать должен
        # гейт приёмки, а гейт видит только этот отчёт. Не написать сюда жанр
        # значит оставить правило в конфиге, куда гейт не заглядывает.
        #
        # Жанр берётся из конфига, то есть из папки, а не из текста. Попытка
        # определять его по содержанию замерена на 169 документах: лучшее — 88 %,
        # пятнадцать настоящих инструкций отвергались бы зря. Отказывать на таком
        # основании нельзя. Подробно — в corpusprep/zhanr.py.
        tekst_proby = None
        try:
            clean_dir = os.path.join(cfg['work_dir'], 'clean', cfg['system'])
            fajly = sorted(f for f in os.listdir(clean_dir) if f.endswith('.md'))
            if fajly:
                tekst_proby = io.open(os.path.join(clean_dir, fajly[0]),
                                      encoding='utf-8').read()
        except OSError:
            tekst_proby = None
        print(json.dumps({
            'ok': code == 0,
            'system': cfg['system'],
            'document_type': cfg.get('document_type'),
            'zhanr': zhanr.proverit(cfg, tekst_proby),
            'out': cfg['out'],
            'stages': [r.data for r in reports],
        }, ensure_ascii=False, indent=1))
    else:
        for r in reports:
            r.emit(as_json=False)
    return code


if __name__ == '__main__':
    sys.exit(main())
