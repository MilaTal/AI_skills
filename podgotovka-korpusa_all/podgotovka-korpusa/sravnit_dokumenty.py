# -*- coding: utf-8 -*-
"""Сравнить документы: одно это или разное, чем отличаются, что новее.

Два режима.

ПАРА — сравнить два названных файла:

    python sravnit_dokumenty.py "путь/один.docx" "путь/два.docx"
    python sravnit_dokumenty.py --json "один.docx" "два.docx"

ПАПКИ — найти похожие пары среди всех документов одной или нескольких папок.
Это то, ради чего умение и заводилось: единственное, на чём держится вся база, —
чтобы в ней не лежали две инструкции об одном без пометки «Неактуально».

    python sravnit_dokumenty.py --papki "Входящие ERP" "Пакет инструкций"
    python sravnit_dokumenty.py --papki --porog 0.3 "папка"

Код возврата: 0 — похожих пар нет и имена соответствуют содержанию; 1 — есть, о
чём доложить человеку; 2 — ошибка входа.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.corpusprep import sravnenie  # noqa: E402


def sobrat_fajly(papki):
    fajly = []
    for p in papki:
        if os.path.isfile(p) and p.lower().endswith('.docx'):
            fajly.append(p)
            continue
        if not os.path.isdir(p):
            print('нет такой папки: %s' % p)
            continue
        for f in sorted(os.listdir(p)):
            if f.lower().endswith('.docx') and not f.startswith('~$'):
                fajly.append(os.path.join(p, f))
    return fajly


def po_papkam(papki, porog):
    """Все пары со сходством выше порога — плюс имена, не сошедшиеся с текстом.

    Сравнение попарное, и это сознательно: корпус у нас в сотни документов, а не
    в десятки тысяч. Отсекать заранее по длине или по названию — значит вводить
    ещё одну догадку туда, где вся ценность в отсутствии догадок. «Список
    изменений» и его короткая часть отличаются вчетверо по длине, и любой отсев
    по размеру их бы развёл.
    """
    fajly = sobrat_fajly(papki)
    if len(fajly) < 2:
        print('нечего сравнивать: файлов %d' % len(fajly))
        return 2
    print('файлов: %d, пар к сравнению: %d'
          % (len(fajly), len(fajly) * (len(fajly) - 1) // 2))

    razbory = {}
    imena_ne_soshlis = []
    for put in fajly:
        try:
            razbory[put] = sravnenie.razobrat(put, sosedi=fajly)
        except Exception as e:
            print('   не прочёлся: %s — %s' % (os.path.basename(put), e))
            continue
        if not razbory[put]['imya_sootvetstvuet']:
            imena_ne_soshlis.append(razbory[put])

    # Один и тот же текст лежит в разных папках: «Обновления ERP» целиком входят
    # во «Входящие ERP». Без отсева каждая пара показывалась бы четыре раза —
    # по разу на каждое сочетание копий, — и в отчёте из шестидесяти пяти пар
    # разных оказалось пятнадцать. Отчёт, который надо чистить глазами, читать
    # перестанут.
    #
    # Отсев идёт по содержанию, а не по имени: копия может лежать под другим
    # именем, и как раз такие пары интереснее всего.
    import hashlib

    def slepok(d):
        return hashlib.md5(
            ('\n'.join(sorted(d['abzacy']))).encode('utf-8')).hexdigest()

    slepki = {put: slepok(d) for put, d in razbory.items()}

    pary, vidennye = [], set()
    puti = sorted(razbory)
    for i in range(len(puti)):
        for j in range(i + 1, len(puti)):
            a, b = razbory[puti[i]], razbory[puti[j]]
            sa, sb = set(a['abzacy']), set(b['abzacy'])
            if not sa or not sb:
                continue
            klyuch = tuple(sorted((slepki[puti[i]], slepki[puti[j]])))
            if klyuch in vidennye:
                continue
            dolya = len(sa & sb) / float(min(len(sa), len(sb)))
            if dolya >= porog:
                # Одинаковые тексты под одинаковым именем — это просто копия
                # файла в другой папке. Отмечаем отдельно, чтобы не мешались
                # среди настоящих находок.
                kopiya = (klyuch[0] == klyuch[1]
                          and a['imya'] == b['imya'])
                vidennye.add(klyuch)
                pary.append((dolya, puti[i], puti[j], kopiya))
    pary.sort(reverse=True)
    kopiy = sum(1 for p in pary if p[3])
    pary = [p for p in pary if not p[3]]

    print()
    if imena_ne_soshlis:
        print('ИМЯ НЕ СОШЛОСЬ С СОДЕРЖАНИЕМ — в очередь оператору: %d'
              % len(imena_ne_soshlis))
        for d in imena_ne_soshlis:
            print('   %s' % d['imya'])
            print('      %s' % d['imya_poyasnenie'])
        print()

    if kopiy:
        print('копий одного файла в разных папках: %d — не показываю' % kopiy)
    if not pary:
        print('похожих пар выше %.2f нет' % porog)
    else:
        print('похожих пар: %d' % len(pary))
        for dolya, pa, pb, _kopiya in pary:
            r = sravnenie.sravnit(pa, pb)
            print()
            print('   %s' % os.path.basename(pa))
            print('   %s' % os.path.basename(pb))
            for stroka in sravnenie.slovami(r):
                print('      ' + stroka)
    return 1 if (pary or imena_ne_soshlis) else 0


def para(put_a, put_b, kak_json):
    for p in (put_a, put_b):
        if not os.path.exists(p):
            print(json.dumps({'ok': False, 'oshibka': 'нет файла: ' + p},
                             ensure_ascii=False) if kak_json
                  else 'нет файла: %s' % p)
            return 2
    r = sravnenie.sravnit(put_a, put_b)
    if kak_json:
        # Даты в JSON не сериализуются сами — переводим в строки, иначе ответ
        # службы падает уже после того, как вся работа сделана.
        def pochistit(d):
            return {k: (str(v) if hasattr(v, 'isoformat') else v)
                    for k, v in d.items()}
        r = dict(r, pervyy=pochistit(dict(r['pervyy'],
                                          daty=pochistit(r['pervyy']['daty']))),
                 vtoroy=pochistit(dict(r['vtoroy'],
                                       daty=pochistit(r['vtoroy']['daty']))))
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print('%s' % os.path.basename(put_a))
        print('%s' % os.path.basename(put_b))
        print()
        for stroka in sravnenie.slovami(r):
            print('   ' + stroka)
        if r['tolko_v_pervom']:
            print()
            print('   только в первом:')
            for s in r['tolko_v_pervom']:
                print('      ' + s[:150])
        if r['tolko_vo_vtorom']:
            print()
            print('   только во втором:')
            for s in r['tolko_vo_vtorom']:
                print('      ' + s[:150])
    return 1 if (r['trebuet_operatora'] or r['dolya_obshchego'] >= 0.4) else 0


def main():
    argv = sys.argv[1:]
    kak_json = '--json' in argv
    po_papkam_rezhim = '--papki' in argv
    porog = 0.4
    if '--porog' in argv:
        i = argv.index('--porog')
        porog = float(argv[i + 1])
        del argv[i:i + 2]
    argv = [a for a in argv if not a.startswith('--')]

    if po_papkam_rezhim:
        return po_papkam(argv, porog)
    if len(argv) != 2:
        print(__doc__)
        return 2
    return para(argv[0], argv[1], kak_json)


if __name__ == '__main__':
    sys.exit(main())
