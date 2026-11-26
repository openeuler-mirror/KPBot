// Linux 独立 session 监督器；禁止负载留下子进程。
import fs from 'node:fs';
import { spawn } from 'node:child_process';
const [limit, base, encoded] = process.argv.slice(2);
const argv = JSON.parse(encoded);
const out = fs.openSync(`${base}.stdout`, 'w'),
  err = fs.openSync(`${base}.stderr`, 'w');
const child = spawn(argv[0], argv.slice(1), { stdio: ['ignore', out, err], detached: true });
let timed = false;
const kill = () => {
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch {}
};
const timer = setTimeout(() => {
  timed = true;
  kill();
}, Number(limit));
child.on('error', (e) => {
  clearTimeout(timer);
  fs.writeSync(err, String(e));
  process.exitCode = 125;
});
child.on('exit', (code, signal) => {
  clearTimeout(timer);
  let remaining = false;
  try {
    process.kill(-child.pid, 0);
    remaining = true;
  } catch {}
  if (remaining) kill();
  if (timed || remaining || signal)
    fs.writeSync(
      err,
      `\nPGO supervisor: timeout=${timed}, remaining_processes=${remaining}, signal=${signal}\n`,
    );
  process.exitCode = timed ? 124 : remaining ? 126 : (code ?? 127);
});
