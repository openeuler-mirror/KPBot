// Copyright (c) 2026 Huawei Technologies Co., Ltd.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace KPBot
{
    // Keep recursive operations on extended Win32 paths even under Windows
    // PowerShell's legacy .NET Framework path handling.
    public static class InstallTree
    {
        private const int FileNotFound = 2;
        private const int PathNotFound = 3;
        private const int NoMoreFiles = 18;
        private const int AlreadyExists = 183;
        private const uint InvalidAttributes = 0xffffffff;
        private const uint NameSurrogateTag = 0x20000000;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct FindData
        {
            public FileAttributes Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint ReparseTag;
            public uint Reserved;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string Name;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]
            public string ShortName;
        }

        private sealed class FindHandle : SafeHandleZeroOrMinusOneIsInvalid
        {
            private FindHandle() : base(true) { }
            protected override bool ReleaseHandle() { return FindClose(handle); }
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern FindHandle FindFirstFileW(string path, out FindData data);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool FindNextFileW(FindHandle handle, out FindData data);
        [DllImport("kernel32.dll", SetLastError = true, ExactSpelling = true)]
        private static extern bool FindClose(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern uint GetFileAttributesW(string path);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool SetFileAttributesW(string path, FileAttributes attributes);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool CreateDirectoryW(string path, IntPtr securityAttributes);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool CopyFileW(string source, string destination, bool failIfExists);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool DeleteFileW(string path);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
        private static extern bool RemoveDirectoryW(string path);

        private static string ExtendedPath(string path)
        {
            // Callers supply absolute filesystem paths resolved by PowerShell.
            path = path.Replace('/', '\\');
            if (path.StartsWith(@"\\?\", StringComparison.Ordinal)) return path;
            if (path.StartsWith(@"\\", StringComparison.Ordinal)) return @"\\?\UNC\" + path.Substring(2);
            return @"\\?\" + path;
        }

        private static IOException FileError(string action, string path, int error)
        {
            var inner = new Win32Exception(error);
            return new IOException(action + " '" + path + "': " + inner.Message, inner);
        }

        private static IEnumerable<FindData> Children(string path)
        {
            FindData entry;
            using (var handle = FindFirstFileW(path + @"\*", out entry))
            {
                if (handle.IsInvalid)
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == FileNotFound) yield break;
                    throw FileError("Cannot list", path, error);
                }

                do
                {
                    if (entry.Name != "." && entry.Name != "..") yield return entry;
                } while (FindNextFileW(handle, out entry));
                int end = Marshal.GetLastWin32Error();
                if (end != NoMoreFiles) throw FileError("Cannot list", path, end);
            }
        }

        public static void Copy(string source, string destination)
        {
            CopyDirectory(ExtendedPath(source), ExtendedPath(destination));
        }

        private static void CopyDirectory(string source, string destination)
        {
            if (!CreateDirectoryW(destination, IntPtr.Zero))
            {
                int error = Marshal.GetLastWin32Error();
                if (error != AlreadyExists) throw FileError("Cannot create directory", destination, error);
                uint destinationAttributes = GetFileAttributesW(destination);
                var kind = (FileAttributes)destinationAttributes & (FileAttributes.Directory | FileAttributes.ReparsePoint);
                if (destinationAttributes == InvalidAttributes || kind != FileAttributes.Directory)
                    throw FileError("Expected a directory", destination, error);
            }

            foreach (var entry in Children(source))
            {
                string child = source + "\\" + entry.Name;
                string target = destination + "\\" + entry.Name;
                if ((entry.Attributes & FileAttributes.Directory) != 0)
                {
                    if ((entry.Attributes & FileAttributes.ReparsePoint) != 0 &&
                        (entry.ReparseTag & NameSurrogateTag) != 0)
                        throw new IOException("Cannot copy a linked skill directory: " + child);
                    CopyDirectory(child, target);
                }
                else if (!CopyFileW(child, target, false))
                {
                    throw FileError("Cannot copy " + child + " to", target, Marshal.GetLastWin32Error());
                }
            }
        }

        public static void Remove(string path)
        {
            path = ExtendedPath(path);
            uint attributes = GetFileAttributesW(path);
            if (attributes == InvalidAttributes)
            {
                int error = Marshal.GetLastWin32Error();
                if (error == FileNotFound || error == PathNotFound) return;
                throw FileError("Cannot inspect", path, error);
            }

            RemoveItem(path, (FileAttributes)attributes);
        }

        private static void RemoveItem(string path, FileAttributes attributes)
        {
            bool isReparsePoint = (attributes & FileAttributes.ReparsePoint) != 0;
            if ((attributes & FileAttributes.Directory) != 0)
            {
                // Removing an NTFS junction or directory link must not traverse its target.
                if (!isReparsePoint)
                {
                    foreach (var entry in Children(path))
                        RemoveItem(path + "\\" + entry.Name, entry.Attributes);
                }
                if (!RemoveDirectoryW(path))
                    throw FileError("Cannot remove directory", path, Marshal.GetLastWin32Error());
                return;
            }

            if (!isReparsePoint && (attributes & FileAttributes.ReadOnly) != 0)
            {
                var writable = attributes & ~FileAttributes.ReadOnly;
                if (!SetFileAttributesW(path, writable == 0 ? FileAttributes.Normal : writable))
                    throw FileError("Cannot clear read-only attribute", path, Marshal.GetLastWin32Error());
            }
            if (!DeleteFileW(path))
                throw FileError("Cannot remove file", path, Marshal.GetLastWin32Error());
        }
    }
}
