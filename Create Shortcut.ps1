# Creates "Twitch Drop Miner" shortcuts (Desktop + Start menu) with the Monika icon.
# The shortcuts carry the app's taskbar ID, so a pinned shortcut and the running window
# share one taskbar button. Re-run this if you move the app folder.
#
#   Right-click > Run with PowerShell   (or: powershell -ExecutionPolicy Bypass -File "Create Shortcut.ps1")

$ErrorActionPreference = 'Stop'
$AppDir = $PSScriptRoot
$AppId = 'Moni.TwitchDropMiner'   # must match APP_ID in twitch_drop_claimer.py

# The real interpreter, not the WindowsApps alias (which can switch versions under you).
$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace TdmShortcut {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    public class CShellLink {}

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    public interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int max, IntPtr fd, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int max);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int max);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int max);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int cmd);
        void SetShowCmd(int cmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int max, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    public interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey {
        public Guid fmtid;
        public uint pid;
        public PropertyKey(Guid g, uint p) { fmtid = g; pid = p; }
    }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr p;
    }

    public static class Maker {
        static PropertyKey AppIdKey = new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

        public static void Create(string lnk, string target, string args, string workdir, string icon,
                                  string desc, string appId) {
            var link = (IShellLinkW)new CShellLink();
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(workdir);
            link.SetIconLocation(icon, 0);
            link.SetDescription(desc);
            var store = (IPropertyStore)link;
            var pv = new PropVariant();
            pv.vt = 31;  // VT_LPWSTR
            pv.p = Marshal.StringToCoTaskMemUni(appId);
            try {
                store.SetValue(ref AppIdKey, ref pv);
                store.Commit();
            } finally {
                Marshal.FreeCoTaskMem(pv.p);
            }
            ((IPersistFile)link).Save(lnk, true);
        }

        public static string ReadAppId(string lnk) {
            var link = (IShellLinkW)new CShellLink();
            ((IPersistFile)link).Load(lnk, 0);
            PropVariant pv;
            ((IPropertyStore)link).GetValue(ref AppIdKey, out pv);
            return pv.vt == 31 ? Marshal.PtrToStringUni(pv.p) : "";
        }
    }
}
"@

$script = Join-Path $AppDir 'twitch_drop_claimer.py'
$icon = Join-Path $AppDir 'assets\icon.ico'
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Twitch Drop Miner.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Twitch Drop Miner.lnk')
)
foreach ($lnk in $targets) {
    [TdmShortcut.Maker]::Create($lnk, $pythonw, "`"$script`"", $AppDir, $icon,
        'Earns and claims Twitch drops automatically', $AppId)
    "Created $lnk  (AppID: $([TdmShortcut.Maker]::ReadAppId($lnk)))"
}
