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

    private static byte[] DecryptAwcInPlace(byte[] raw)
    {
        if (raw == null || raw.Length < 16) return raw;
        byte[] data = (byte[])raw.Clone();

        uint magic = BitConverter.ToUInt32(data, 0);
        if (magic != 0x54414441 && magic != 0x41444154)
        {
            if (data.Length % 4 == 0)
            {
                AwcFile.Decrypt_RSXXTEA(data);
                magic = BitConverter.ToUInt32(data, 0);
            }
        }
        if (magic != 0x54414441) return data;

        ushort flags = BitConverter.ToUInt16(data, 6);
        int streamCount = BitConverter.ToInt32(data, 8);
        bool chunkIndices = (flags & 1) == 1;
        bool singleEnc    = (flags & 2) == 2;
        bool multiChan    = (flags & 4) == 4;
        bool multiEnc     = (flags & 8) == 8;

        if (!singleEnc && !multiEnc) return data;

        int pos = 16 + (chunkIndices ? streamCount * 2 : 0);
        uint[] streamInfos = new uint[streamCount];
        for (int i = 0; i < streamCount; i++)
        {
            streamInfos[i] = BitConverter.ToUInt32(data, pos);
            pos += 4;
        }

        int blockSize = 0;
        int blockCount = 0;
        var dataChunks = new System.Collections.Generic.List<(int offset, int size)>();

        for (int i = 0; i < streamCount; i++)
        {
            uint cCount = streamInfos[i] >> 29;
            for (int j = 0; j < cCount; j++)
            {
                ulong rawVal = BitConverter.ToUInt64(data, pos);
                pos += 8;
                byte cType = (byte)(rawVal >> 56);
                int cSize  = (int)((rawVal >> 28) & 0x0FFFFFFF);
                int cOff   = (int)(rawVal & 0x0FFFFFFF);

                if (cType == 0x48 && cSize >= 12) // streamformat
                {
                    blockCount = BitConverter.ToInt32(data, cOff);
                    blockSize  = BitConverter.ToInt32(data, cOff + 4);
                }
                else if (cType == 0x55 && cSize > 0) // data
                {
                    dataChunks.Add((cOff, cSize));
                }
            }
        }

        if (multiChan && multiEnc && blockSize > 0)
        {
            foreach (var dc in dataChunks)
            {
                for (int b = 0; b < blockCount; b++)
                {
                    int srcOff = dc.offset + b * blockSize;
                    int bLen = Math.Max(0, Math.Min(blockSize, dc.offset + dc.size - srcOff));
                    if (bLen > 0 && bLen % 4 == 0)
                    {
                        byte[] blk = new byte[bLen];
                        Buffer.BlockCopy(data, srcOff, blk, 0, bLen);
                        AwcFile.Decrypt_RSXXTEA(blk);
                        Buffer.BlockCopy(blk, 0, data, srcOff, bLen);
                    }
                }
            }
            flags = (ushort)(flags & ~8);
            BitConverter.GetBytes(flags).CopyTo(data, 6);
        }
        else if (!multiChan && singleEnc)
        {
            foreach (var dc in dataChunks)
            {
                if (dc.size >= 2 && data[dc.offset] == 0xFF && (data[dc.offset + 1] & 0xE0) == 0xE0)
                    continue;
                if (dc.size % 4 == 0)
                {
                    byte[] chunk = new byte[dc.size];
                    Buffer.BlockCopy(data, dc.offset, chunk, 0, dc.size);
                    AwcFile.Decrypt_RSXXTEA(chunk);
                    Buffer.BlockCopy(chunk, 0, data, dc.offset, dc.size);
                }
            }
            flags = (ushort)(flags & ~2);
            BitConverter.GetBytes(flags).CopyTo(data, 6);
        }

        return data;
    }

    private static int Main(string[] args)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
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

        LoadKeys(gameDir);

        var rpf = new RpfFile(rpfPath, Path.GetFileName(rpfPath));
        rpf.ScanStructure(null, err => Console.WriteLine("RPF Error: " + err));

        if (rpf.AllEntries == null)
        {
            Console.WriteLine("ERROR: Could not scan RPF entries.");
            return 2;
        }

        if (cmd == "list")
        {
            Console.WriteLine($"RPF {rpf.Name}: Encryption={rpf.Encryption}, Entries={rpf.AllEntries.Count}");
            foreach (var e in rpf.AllEntries)
            {
                if (e is RpfFileEntry fe)
                    Console.WriteLine($"  [FILE] {fe.Path} ({fe.GetFileSize()} bytes)");
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
                    if (targetName.EndsWith(".awc"))
                    {
                        data = DecryptAwcInPlace(data);
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outFile)));
                    File.WriteAllBytes(outFile, data);
                    Console.WriteLine($"EXTRACTED (decrypted): {fe.Path} ({data.Length} bytes) -> {outFile}");
                    return 0;
                }
            }
            Console.WriteLine($"ERROR: Entry '{targetName}' not found in {rpfPath}");
            return 4;
        }
        else if (cmd == "inject" && args.Length >= 4)
        {
            string targetDir = args.Length >= 5 ? args[3].ToLowerInvariant().Replace('/', '\\').Trim('\\') : "";
            if (targetDir == "." || targetDir == "root") targetDir = "";
            string inFile = args.Length >= 5 ? args[4] : args[3];
            string fileName = Path.GetFileName(inFile);
            string fileNameLower = fileName.ToLowerInvariant();
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

            // Fast in-place binary overwrite when replacing an existing uncompressed entry of identical byte size!
            // This preserves the original Rockstar NG-encrypted RPF header untouched (< 1 ms, no OpenIV.asi required!).
            foreach (var ex in dirEntry.Files)
            {
                if (ex is RpfBinaryFileEntry bfe && bfe.NameLower == fileNameLower && bfe.FileSize == 0 && !bfe.IsEncrypted)
                {
                    long origLen = bfe.GetFileSize();
                    if (origLen == data.Length)
                    {
                        long writePos = rpf.StartPos + ((long)bfe.FileOffset * 512);
                        using (var fs = File.Open(rpfPath, FileMode.Open, FileAccess.ReadWrite))
                        {
                            fs.Position = writePos;
                            fs.Write(data, 0, data.Length);
                        }
                        Console.WriteLine($"INJECTED IN-PLACE (native NG header preserved, 0 header changes): {bfe.Path} ({data.Length} bytes @ offset 0x{writePos:X})");
                        return 0;
                    }
                }
            }

            rpf.Encryption = RpfEncryption.OPEN;
            var created = RpfFile.CreateFile(dirEntry, fileName, data, overwrite: true);
            Console.WriteLine($"INJECTED (OPEN header): {created.Path} ({data.Length} bytes)");
            return 0;
        }

        return 1;
    }
}
