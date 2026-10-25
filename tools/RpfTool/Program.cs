using System;
using System.IO;
using System.Linq;
using System.Reflection;
using CodeWalker.GameFiles;

internal static class Program
{
    private static void LoadKeys(string gameDir)
    {
        foreach (var m in typeof(GTA5Keys).GetMethods(BindingFlags.Public | BindingFlags.Static))
        {
            if (m.Name == "LoadFromPath")
            {
                var ps = m.GetParameters();
                if (ps.Length == 3)
                    m.Invoke(null, new object[] { gameDir, false, "" });
                else if (ps.Length == 2 && ps[1].ParameterType == typeof(string))
                    m.Invoke(null, new object[] { gameDir, "" });
                else if (ps.Length == 2 && ps[1].ParameterType == typeof(bool))
                    m.Invoke(null, new object[] { gameDir, false });
                else if (ps.Length == 1)
                    m.Invoke(null, new object[] { gameDir });
                return;
            }
        }
    }

    private static int Main(string[] args)
    {
        if (args.Length < 3)
        {
            Console.WriteLine("Usage:");
            Console.WriteLine("  RpfTool list    <gameDir> <rpfPath>");
            Console.WriteLine("  RpfTool extract <gameDir> <rpfPath> <entryName> <outFile>");
            Console.WriteLine("  RpfTool inject  <gameDir> <rpfPath> <entryDirPath> <inFile>");
            return 1;
        }

        string cmd = args[0].ToLowerInvariant();
        string gameDir = args[1];
        string rpfPath = args[2];

        try
        {
            LoadKeys(gameDir);
        }
        catch (Exception ex)
        {
            Console.WriteLine("Warning in LoadKeys: " + ex.Message);
        }

        var rpf = new RpfFile(rpfPath, Path.GetFileName(rpfPath));
        rpf.ScanStructure(null, err => Console.WriteLine("RPF Error: " + err));

        if (rpf.AllEntries == null)
        {
            Console.WriteLine("ERROR: Could not scan RPF entries (AllEntries is null).");
            return 2;
        }

        if (cmd == "list")
        {
            Console.WriteLine($"RPF {rpf.Name}: Encryption={rpf.Encryption}, Entries={rpf.AllEntries.Count}");
            foreach (var e in rpf.AllEntries)
            {
                if (e is RpfFileEntry fe)
                {
                    Console.WriteLine($"  [FILE] {fe.Path} ({fe.GetFileSize()} bytes)");
                }
            }
            return 0;
        }
        else if (cmd == "extract" && args.Length >= 5)
        {
            string targetName = args[3].ToLowerInvariant();
            string outFile = args[4];
            foreach (var e in rpf.AllEntries)
            {
                if (e is RpfFileEntry fe && (fe.NameLower == targetName || fe.Path.ToLowerInvariant().EndsWith(targetName)))
                {
                    byte[] data = rpf.ExtractFile(fe);
                    if (data == null)
                    {
                        Console.WriteLine($"ERROR: ExtractFile returned null for {fe.Path}");
                        return 3;
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outFile)));
                    File.WriteAllBytes(outFile, data);
                    Console.WriteLine($"EXTRACTED: {fe.Path} ({data.Length} bytes) -> {outFile}");
                    return 0;
                }
            }
            Console.WriteLine($"ERROR: Entry '{targetName}' not found in {rpfPath}");
            return 4;
        }
        else if (cmd == "inject" && args.Length >= 5)
        {
            string targetDir = args[3].ToLowerInvariant().Replace('/', '\\').Trim('\\');
            string inFile = args[4];
            string fileName = Path.GetFileName(inFile);
            byte[] data = File.ReadAllBytes(inFile);

            RpfDirectoryEntry dirEntry = rpf.Root;
            if (!string.IsNullOrEmpty(targetDir))
            {
                foreach (var e in rpf.AllEntries)
                {
                    if (e is RpfDirectoryEntry de && de.Path.ToLowerInvariant().EndsWith(targetDir))
                    {
                        dirEntry = de;
                        break;
                    }
                }
            }

            rpf.Encryption = RpfEncryption.NG;
            var created = RpfFile.CreateFile(dirEntry, fileName, data, overwrite: true);
            Console.WriteLine($"INJECTED: {created.Path} ({data.Length} bytes) with native NG encryption!");
            return 0;
        }

        Console.WriteLine("Invalid arguments.");
        return 1;
    }
}
