# Растеризация векторных экранов EMF/WMF в PNG.
#
# Зачем это есть. Word хранит схемы процессов и врисованные объекты не
# картинкой, а метафайлом Windows. Распознавание принимает только растр, и
# такие экраны молча выпадали: в отчёте «формат не поддерживается», в базе —
# дырка на месте схемы. В методике ERP так терялось 127 экранов из 362, и среди
# них все схемы процессов — то самое «как устроено», ради чего методику и брали.
#
# Почему PowerShell, а не Python. EMF — собственный формат Windows, и
# единственный надёжный его рисовальщик здесь же, в System.Drawing. Ставить
# ради этого стороннюю библиотеку и тащить её в окружение дороже, чем вызвать
# то, что уже есть в системе.
#
# Масштаб 2 не для красоты: метафайл хранит размер в пунктах, и при отрисовке
# один-в-один подписи на схеме выходят мелкими до нечитаемости.
param(
    [Parameter(Mandatory = $true)][string]$Papka,
    [int]$Masshtab = 2
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$sdelano = 0
$ne_vyshlo = 0

Get-ChildItem -LiteralPath $Papka -File | Where-Object {
    $_.Extension -match '^\.(emf|wmf)$'
} | ForEach-Object {
    $src = $_.FullName
    # Имя растра — исходное ПЛЮС .png, а не вместо расширения. В одной папке
    # рядом с image1.emf может лежать свой image1.png, и подмена расширения
    # затёрла бы чужой экран.
    $dst = $src + '.png'
    if (Test-Path -LiteralPath $dst) {
        $sdelano += 1
        return
    }
    try {
        $mf = [System.Drawing.Imaging.Metafile]::new($src)
        $w = [int][Math]::Max(1, $mf.Width * $Masshtab)
        $h = [int][Math]::Max(1, $mf.Height * $Masshtab)
        $bmp = [System.Drawing.Bitmap]::new($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        # Белая подложка обязательна: метафайл обычно прозрачен, а на прозрачном
        # фоне PNG чёрный текст схемы уезжает в чёрный фон и не читается.
        $g.Clear([System.Drawing.Color]::White)
        $g.InterpolationMode = 'HighQualityBicubic'
        $g.SmoothingMode = 'HighQuality'
        $g.DrawImage($mf, (New-Object System.Drawing.Rectangle 0, 0, $w, $h))
        $g.Dispose()
        $bmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $mf.Dispose()
        $sdelano += 1
    }
    catch {
        $ne_vyshlo += 1
        Write-Output ("не вышло`t" + $_.Exception.Message + "`t" + $src)
    }
}

Write-Output ("итог`t{0}`t{1}" -f $sdelano, $ne_vyshlo)
