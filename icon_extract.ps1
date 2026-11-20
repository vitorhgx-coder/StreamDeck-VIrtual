# Extrai o icone real de um .exe / .lnk / .ico e salva como PNG.
# Chamado pelo server.py: variaveis de ambiente SD_SRC (origem), SD_OUT (saida .png), SD_SIZE (px).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$src = $env:SD_SRC
$out = $env:SD_OUT
$px = 128
if ($env:SD_SIZE) { $px = [int]$env:SD_SIZE }

# Atalho .lnk: descobre de onde vem o icone (IconLocation ou o proprio alvo)
if ($src.ToLower().EndsWith('.lnk')) {
  try {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($src)
    $cand = $null
    $loc = [string]$lnk.IconLocation
    if ($loc) {
      $p = [Environment]::ExpandEnvironmentVariables($loc.Split(',')[0].Trim())
      if ($p -and (Test-Path -LiteralPath $p)) { $cand = $p }
    }
    if (-not $cand) {
      $t = [string]$lnk.TargetPath
      if ($t -and (Test-Path -LiteralPath $t)) { $cand = $t }
    }
    if ($cand) { $src = $cand }
  } catch { }
}

$cs = @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class SdIcon {
  [StructLayout(LayoutKind.Sequential)] public struct SIZE { public int cx; public int cy; }
  [StructLayout(LayoutKind.Sequential)] public struct BITMAPINFOHEADER {
    public int biSize; public int biWidth; public int biHeight; public short biPlanes; public short biBitCount;
    public int biCompression; public int biSizeImage; public int biXPelsPerMeter; public int biYPelsPerMeter;
    public int biClrUsed; public int biClrImportant;
  }
  [StructLayout(LayoutKind.Sequential)] public struct BITMAP {
    public int bmType; public int bmWidth; public int bmHeight; public int bmWidthBytes;
    public short bmPlanes; public short bmBitsPixel; public IntPtr bmBits;
  }
  [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IShellItemImageFactory {
    [PreserveSig] int GetImage(SIZE size, int flags, out IntPtr phbm);
  }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName([MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr pbc, ref Guid riid,
      [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory ppv);
  [DllImport("gdi32.dll")] static extern int GetObject(IntPtr h, int cb, ref BITMAP bm);
  [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr h);
  [DllImport("gdi32.dll")] static extern int GetDIBits(IntPtr hdc, IntPtr hbm, uint start, uint lines, byte[] bits, ref BITMAPINFOHEADER bi, uint usage);
  [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hwnd);
  [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd, IntPtr hdc);

  public static bool Save(string src, string dst, int px) {
    Guid iid = new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
    IShellItemImageFactory f;
    SHCreateItemFromParsingName(src, IntPtr.Zero, ref iid, out f);
    SIZE sz = new SIZE(); sz.cx = px; sz.cy = px;
    IntPtr hbm;
    int hr = f.GetImage(sz, 0x4 | 0x1, out hbm);   // ICONONLY | BIGGERSIZEOK
    if (hr != 0 || hbm == IntPtr.Zero) return false;
    try {
      BITMAP bm = new BITMAP();
      GetObject(hbm, Marshal.SizeOf(typeof(BITMAP)), ref bm);
      int w = bm.bmWidth, h = bm.bmHeight;
      if (w <= 0 || h <= 0) return false;
      BITMAPINFOHEADER bi = new BITMAPINFOHEADER();
      bi.biSize = Marshal.SizeOf(typeof(BITMAPINFOHEADER));
      bi.biWidth = w; bi.biHeight = -h; bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0;
      byte[] data = new byte[w * h * 4];
      IntPtr dc = GetDC(IntPtr.Zero);
      int lines = GetDIBits(dc, hbm, 0, (uint)h, data, ref bi, 0);
      ReleaseDC(IntPtr.Zero, dc);
      if (lines == 0) return false;
      bool anyAlpha = false;
      for (int i = 3; i < data.Length; i += 4) { if (data[i] != 0) { anyAlpha = true; break; } }
      if (!anyAlpha) return false;
      GCHandle gh = GCHandle.Alloc(data, GCHandleType.Pinned);
      try {
        using (Bitmap tmp = new Bitmap(w, h, w * 4, PixelFormat.Format32bppPArgb, gh.AddrOfPinnedObject()))
        using (Bitmap res = new Bitmap(w, h, PixelFormat.Format32bppArgb)) {
          using (Graphics g = Graphics.FromImage(res)) { g.Clear(Color.Transparent); g.DrawImage(tmp, 0, 0, w, h); }
          res.Save(dst, ImageFormat.Png);
        }
      } finally { gh.Free(); }
      return true;
    } finally { DeleteObject(hbm); }
  }
}
'@

$ok = $false
try {
  Add-Type -TypeDefinition $cs -ReferencedAssemblies System.Drawing
  $ok = [SdIcon]::Save($src, $out, $px)
} catch { $ok = $false }

if (-not $ok) {
  # Plano B (mais simples, 32x32): icone associado ao arquivo
  $ic = [System.Drawing.Icon]::ExtractAssociatedIcon($src)
  $bmp = $ic.ToBitmap()
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
}
