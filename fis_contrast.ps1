param(
  [string]$Port = 'COM1',
  [int]$Shift = 0,            # make the FIS darker by N steps: subtracts N from all ten contrast bytes (keeps the temperature curve)
  [string]$Restore,           # write the contrast row back from a backup made by this script
  [switch]$Write,             # without this the script only reads, backs up and shows the table
  [int]$Floor = 0x10,         # never write a contrast byte below this (0x10 already ghosts at room temperature)
  [int]$PreReqMs = 5
)
# RxD FIS Adjuster: B5 Audi VDO cluster (8D0 920 9xx, "B5-KOMBIINSTR. VDO") FIS/centre display contrast.
# The contrast curve is EEPROM 0x26A: count (0A), 10 x breakpoints (0x26B-0x274), 10 contrast bytes (0x275-0x27E).
# Lower contrast byte = more LCD drive = darker/stronger pixels. Factory is 50 50 50 50 50 50 4D 4D 40 1F.
# Talks over a plain FTDI "KKL" K-line cable: KW1281 0x17, mode login "loginas9", then 19200 8E1 "D1" commands.
$ErrorActionPreference = 'Stop'
$XAXIS = @(0x0A, 0x21, 0x3E, 0x79, 0xA5, 0xB7, 0xBF, 0xC6, 0xCC, 0xD1, 0xD6)   # 0x26A-0x274, identical on every unit seen
$sp = New-Object System.IO.Ports.SerialPort $Port, 10400, 'None', 8, 'One'
$sp.DtrEnable = $true; $sp.RtsEnable = $false
$sp.Open()
$script:cnt = 0

function Wait-Ms([double]$ms) { $sw = [Diagnostics.Stopwatch]::StartNew(); while ($sw.Elapsed.TotalMilliseconds -lt $ms) {} }   # busy-wait: Sleep() jitter breaks the 5-baud init
function Read-Byte([int]$t = 500) { $sp.ReadTimeout = $t; try { $sp.ReadByte() } catch [TimeoutException] { -1 } }
function Hex($b) { ($b | ForEach-Object { '{0:X2}' -f $_ }) -join ' ' }
function Put([int]$b) { $sp.Write([byte[]]@($b), 0, 1); [void](Read-Byte 100) }   # write + swallow echo

# ---- KW1281 part (10400 baud) ----
function Init-5Baud {
  $sp.DiscardInBuffer(); $sp.BreakState = $false; Wait-Ms 300
  $bits = @(0); for ($i = 0; $i -lt 7; $i++) { $bits += ((0x17 -shr $i) -band 1) }
  $p = 1; foreach ($b in $bits[1..7]) { $p = $p -bxor $b }; $bits += $p; $bits += 1
  foreach ($bit in $bits) { $sp.BreakState = ($bit -eq 0); Wait-Ms 200 }
  $sp.BreakState = $false; $sp.DiscardInBuffer()
  $sync = Read-Byte 1500; if ($sync -ne 0x55) { throw "No sync (got $sync)" }
  $kb1 = Read-Byte 200; $kb2 = Read-Byte 200
  Wait-Ms 30; Put ((-bnot $kb2) -band 0xFF)
  if ($kb1 -ne 0x01 -or $kb2 -ne 0x8A) { throw ("unexpected keywords {0:X2} {1:X2}" -f $kb1, $kb2) }
}
function Recv-Block {
  $len = Read-Byte 1500; if ($len -lt 0) { throw 'timeout waiting for block' }
  $blk = @($len)
  for ($i = 1; $i -lt $len; $i++) {
    Wait-Ms 2; Put ((-bnot $blk[-1]) -band 0xFF)
    $b = Read-Byte 500; if ($b -lt 0) { throw "timeout in block after $(Hex $blk)" }
    $blk += $b
  }
  Wait-Ms 2; Put ((-bnot $blk[-1]) -band 0xFF)
  $end = Read-Byte 500; if ($end -ne 0x03) { throw "bad block end $end after $(Hex $blk)" }
  $script:cnt = $blk[1]
  return , ([int[]]$blk)
}
function Send-Block([int]$title, [int[]]$data = @()) {
  $script:cnt = ($script:cnt + 1) -band 0xFF
  $blk = @((3 + $data.Count), $script:cnt, $title) + $data
  Wait-Ms 25
  foreach ($b in $blk) {
    Put $b
    $c = Read-Byte 500
    if ($c -ne ((-bnot $b) -band 0xFF)) { throw "no complement for $('{0:X2}' -f $b) (got $c)" }
  }
  Put 0x03
}
function Ascii($d) { -join ($d | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } }) }

# ---- D1 part (19200 baud): D1 <len incl. D1 and xor> <cmd> <data..> <xor of all previous bytes> ----
function D1([int[]]$body, [int]$tries = 8) {
  $pkt = @(0xD1, ($body.Count + 3)) + $body
  $x = 0; foreach ($b in $pkt) { $x = $x -bxor $b }; $pkt += $x
  for ($t = 1; $t -le $tries; $t++) {
    Wait-Ms $PreReqMs
    $sp.DiscardInBuffer()
    $sp.Write([byte[]]$pkt, 0, $pkt.Count)
    $echo = @(); for ($i = 0; $i -lt $pkt.Count; $i++) { $echo += (Read-Byte 200) }
    if ((Hex $echo) -ne (Hex $pkt)) { "  echo mismatch on try ${t}: sent $(Hex $pkt) got $(Hex $echo)" | Write-Host; Wait-Ms (50 * $t); continue }
    $h = Read-Byte 300; $n = Read-Byte 100
    if ($h -ne 0xD1 -or $n -lt 3) { "  no reply to $(Hex $pkt) on try $t (got $h $n)" | Write-Host; Wait-Ms (50 * $t); continue }
    $r = @($h, $n); for ($i = 2; $i -lt $n; $i++) { $r += (Read-Byte 100) }
    $x = 0; foreach ($b in $r[0..($n - 2)]) { $x = $x -bxor $b }
    if ($r -contains -1 -or $x -ne $r[-1]) { "  bad reply on try ${t}: $(Hex $r)" | Write-Host; Wait-Ms (50 * $t); continue }
    return , ([int[]]$r)
  }
  throw "D1 command $(Hex $body) failed after $tries tries"
}
function Read-All {
  $img = New-Object byte[] 2048
  for ($a = 0; $a -lt 2048; $a += 16) {
    $r = D1 @(0x72, 0x10, ($a -shr 8), ($a -band 0xFF))
    if ($r[2] -ne 0x72 -or $r.Count -ne 20) { throw "bad read reply at $('{0:X3}' -f $a): $(Hex $r)" }
    for ($i = 0; $i -lt 16; $i++) { $img[$a + $i] = [byte]$r[3 + $i] }
  }
  return , $img
}

function Login {
  $sp.BaudRate = 10400; $sp.Parity = 'None'; $script:cnt = 0
  for ($t = 1; $t -le 3; $t++) { try { Init-5Baud; break } catch { "init try ${t}: $_"; if ($t -eq 3) { throw } } }
  # ident, ACK each block, until the cluster ACKs
  $id = @(); while ($true) { $b = Recv-Block; if ($b[2] -eq 0x09) { break }; $id += , $b; Send-Block 0x09 }
  "IDENT: " + (($id | ForEach-Object { Ascii $_[3..($_.Count - 1)] }) -join ' | ')
  if ((Ascii $id[0][3..12]) -notlike '8D0920*' -or (Ascii $id[2][3..($id[2].Count - 1)]) -notlike '*VDO*') { throw 'not a B5 VDO cluster, stopping' }
  # VAG.exe asks for ident again (title 00), ACKs three blocks, then answers the 4th with the mode login
  Send-Block 0x00
  for ($i = 1; $i -le 3; $i++) { [void](Recv-Block); Send-Block 0x09 }
  [void](Recv-Block)
  Send-Block 0x1B (@(0x80) + [int[]][char[]]'loginas9')
  $r = Recv-Block; if ($r[2] -ne 0x09) { throw "mode login refused: $(Hex $r)" }
  "mode login ACKed"
  Wait-Ms 1600
  $sp.BaudRate = 19200; $sp.Parity = 'Even'; $sp.DiscardInBuffer()   # D1 mode is 19200 8E1
  $v = D1 @(0x49); "D1 49: $(Hex $v)"
  # password table from VAG.exe 0x4CB34E, chosen by the version byte (reply byte 4): D11 = 0x47 -> vdokombi, D13 = 0x59 -> w10kombi
  $ver = $v[4]
  $pw = if ($ver -eq 0x56) { 'w10serie' } elseif ($ver -ge 0x57 -and $ver -le 0x59) { 'w10kombi' } elseif ($ver -ge 0x70) { '19xDR8xS' } else { 'vdokombi' }
  "version byte 0x{0:X2} -> password {1}" -f $ver, $pw
  $r = D1 (@(0x53, 0x9D) + [int[]][char[]]$pw); if ($r[2] -ne 0x06) { throw "$pw login refused: $(Hex $r)" }; "$pw login ACKed"
  $r = D1 @(0x53, 0x96, 0x04); "D1 53 96 04: $(Hex $r)"
}

try {
  Login
  "reading EEPROM twice ..."
  $a = Read-All; $b = Read-All
  for ($i = 0; $i -lt 2048; $i++) { if ($a[$i] -ne $b[$i] -and $i -notin 0x294, 0x6E9) { throw "the two reads differ at $('{0:X3}' -f $i): bad connection, nothing written" } }
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $bak = "fis_backup_$stamp.bin"; [IO.File]::WriteAllBytes($bak, $a); "backup saved: $bak"
  for ($i = 0; $i -lt 11; $i++) { if ($a[0x26A + $i] -ne $XAXIS[$i]) { throw "EEPROM 0x26A-0x274 is $(Hex $a[0x26A..0x274]), not the known layout. Unknown cluster, nothing written." } }
  $cur = [int[]]$a[0x275..0x27E]
  "x breakpoints : $(Hex $a[0x26B..0x274])"
  "contrast now  : $(Hex $cur)   (factory 50 50 50 50 50 50 4D 4D 40 1F, lower = darker)"
  if ($Restore) {
    $r = [IO.File]::ReadAllBytes($Restore); if ($r.Length -ne 2048) { throw "$Restore is not a 2048-byte backup" }
    $new = [int[]]$r[0x275..0x27E]
  } elseif ($Shift -ne 0) {
    $new = $cur | ForEach-Object { [Math]::Min(0xFF, [Math]::Max($Floor, $_ - $Shift)) }
  } else { "read only. Use -Shift N -Write to change it, or -Restore <backup> -Write."; return }
  "contrast new  : $(Hex $new)"
  if (-not $Write) { "dry run: add -Write to apply"; return }
  $row = [int[]]$a[0x270..0x27F]; for ($i = 0; $i -lt 10; $i++) { $row[5 + $i] = $new[$i] }
  $r = D1 (@(0x77, 0x10, 0x02, 0x70) + $row) 1    # one try only: never resend a write blind
  if ($r[2] -ne 0x06) { throw "write not ACKed: $(Hex $r). Run again to re-read before anything else." }
  $r = D1 @(0x53, 0x82)                            # same as VAG EEPROM Programmer after a write; the FIS picks up the new value
  "written. The cluster restarts its display now; logging in again to verify ..."
  $sp.Close(); Start-Sleep -Seconds 4; $sp.Open()
  for ($t = 1; $t -le 3; $t++) { try { Login; break } catch { "re-login try ${t}: $_"; if ($t -eq 3) { throw "could not log in again to verify. Run the script with no options to read it back." }; $sp.Close(); Start-Sleep -Seconds 3; $sp.Open() } }
  $c = Read-All
  $bad = @(); for ($i = 0; $i -lt 2048; $i++) { $want = if ($i -ge 0x275 -and $i -le 0x27E) { $new[$i - 0x275] } else { $a[$i] }; if ($c[$i] -ne $want -and $i -notin 0x294, 0x6E9) { $bad += ('{0:X3}' -f $i) } }
  if ($bad) { throw "VERIFY FAILED at $($bad -join ' '). Restore with -Restore $bak -Write" }
  "written and verified. Undo: -Restore $bak -Write"
} finally { $sp.BreakState = $false; $sp.Close() }
