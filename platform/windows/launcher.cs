// SPDX-License-Identifier: MPL-2.0

using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class Launcher
{
    private static int Main(string[] args)
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string desktop = Path.Combine(directory, "corinthos-desktop.exe");
        try
        {
            if (args.Length == 0 && File.Exists(desktop))
            {
                RequireOrdinary(desktop);
                using (Process child = Process.Start(new ProcessStartInfo(desktop)
                {
                    WorkingDirectory = directory,
                    UseShellExecute = false
                }))
                {
                    return 0;
                }
            }
            string executable = Path.GetFullPath(Path.Combine(directory, "..", "runtime", "python.exe"));
            RequireOrdinary(executable);
            var arguments = new StringBuilder("-I -B -X utf8 -m corinthos");
            foreach (string arg in args)
            {
                arguments.Append(' ');
                AppendQuoted(arguments, arg);
            }

            var start = new ProcessStartInfo(executable, arguments.ToString()) { UseShellExecute = false };
            // The child shares this console and handles its own interrupt.
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs signal)
            {
                signal.Cancel = true;
            };
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            if (!(error is Win32Exception || error is IOException || error is UnauthorizedAccessException))
                throw;
            Console.Error.WriteLine("Corinthos could not start its installed executable. Check the installation or rerun setup.");
            return 1;
        }
    }

    private static void RequireOrdinary(string executable)
    {
        var file = File.GetAttributes(executable);
        if ((file & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0)
            throw new IOException("Invalid installed executable.");
        for (var parent = new FileInfo(executable).Directory; parent != null; parent = parent.Parent)
            if ((parent.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Invalid installed directory.");
    }

    private static void AppendQuoted(StringBuilder target, string value)
    {
        target.Append('"');
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }

            target.Append('\\', character == '"' ? 2 * backslashes + 1 : backslashes);
            target.Append(character);
            backslashes = 0;
        }
        target.Append('\\', 2 * backslashes).Append('"');
    }
}
