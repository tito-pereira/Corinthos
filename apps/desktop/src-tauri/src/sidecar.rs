// SPDX-License-Identifier: MPL-2.0
//! Owns one Python sidecar and exchanges bounded JSON-RPC messages over stdio.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::env;
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, Receiver, SyncSender},
};
use std::thread;
use std::time::{Duration, Instant};

const MAX_FRAME_BYTES: usize = 65_536;
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(8);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const PROJECT_OPEN_TIMEOUT: Duration = Duration::from_secs(15);
const SWITCH_EXECUTE_TIMEOUT: Duration = Duration::from_secs(20);
const IMPORT_INSPECT_TIMEOUT: Duration = Duration::from_secs(45);
const IMPORT_WRITE_TIMEOUT: Duration = Duration::from_secs(120);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);
const SESSION_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(12);
const PROTOCOL_VERSION: u32 = 2;
const CORE_METHODS: [&str; 5] = [
    "system.handshake",
    "system.version",
    "project.status",
    "providers.status",
    "system.shutdown",
];
type Result<T> = std::result::Result<T, &'static str>;
pub type EventSink = Arc<dyn Fn(LifecycleEvent) + Send + Sync>;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Handshake {
    pub protocol_version: u32,
    pub version: String,
    pub methods: Vec<String>,
}

impl Handshake {
    pub fn supports(&self, method: &str) -> bool {
        self.methods.iter().any(|value| value == method)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LifecycleEvent {
    pub method: &'static str,
    pub params: Value,
}

enum Message {
    Ready,
    Response(u64, Result<Value>),
}

pub struct Sidecar {
    child: Child,
    requests: Option<SyncSender<Vec<u8>>>,
    messages: Receiver<Message>,
    closing: Arc<AtomicBool>,
    events: EventSink,
    next_id: u64,
    shutdown_timeout: Duration,
}

impl Sidecar {
    pub fn start(events: EventSink) -> Result<(Self, Handshake)> {
        Self::launch(
            python_command(&resolve_python()?, &["-m", "corinthos.application.sidecar"]),
            events,
            HANDSHAKE_TIMEOUT,
        )
    }

    fn launch(
        mut command: Command,
        events: EventSink,
        timeout: Duration,
    ) -> Result<(Self, Handshake)> {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "Could not start the Corinthos sidecar.")?;
        let mut input = child.stdin.take().expect("spawned with piped stdin");
        let output = child.stdout.take().expect("spawned with piped stdout");
        let (requests, outgoing) = mpsc::sync_channel::<Vec<u8>>(1);
        let (incoming, messages) = mpsc::sync_channel(4);
        let closing = Arc::new(AtomicBool::new(false));
        let reader_closing = Arc::clone(&closing);
        let reader_events = Arc::clone(&events);
        let mut client = Self {
            child,
            requests: Some(requests),
            messages,
            closing,
            events: Arc::clone(&events),
            next_id: 1,
            shutdown_timeout: SHUTDOWN_TIMEOUT,
        };
        thread::Builder::new()
            .spawn(move || {
                while let Ok(bytes) = outgoing.recv() {
                    if input
                        .write_all(&bytes)
                        .and_then(|()| input.flush())
                        .is_err()
                    {
                        break;
                    }
                }
            })
            .map_err(|_| "Could not start the sidecar writer.")?;
        thread::Builder::new()
            .spawn(move || {
                let mut stream = BufReader::new(output);
                let mut ready = false;
                while let Ok(Some(frame)) = read_frame(&mut stream) {
                    match decode(&frame) {
                        Ok(Wire::Ready) if !ready => {
                            ready = true;
                            if incoming.send(Message::Ready).is_err() {
                                break;
                            }
                        }
                        Ok(Wire::Stopping) => reader_events(LifecycleEvent {
                            method: "system.stopping",
                            params: json!({}),
                        }),
                        Ok(Wire::Response(id, result)) => {
                            if incoming.send(Message::Response(id, result)).is_err() {
                                break;
                            }
                        }
                        _ => break,
                    }
                }
                drop(incoming);
                if ready {
                    disconnected(&reader_closing, &reader_events);
                }
            })
            .map_err(|_| "Could not start the sidecar reader.")?;

        let deadline = Instant::now() + timeout;
        client.send(
            "system.handshake",
            json!({"protocol_version": PROTOCOL_VERSION}),
        )?;
        let Ok(Message::Ready) = client.receive(deadline) else {
            return Err("The Corinthos sidecar did not announce ready.");
        };
        let handshake: Handshake = serde_json::from_value(client.receive_response(1, deadline)?)
            .map_err(|_| "Invalid Corinthos sidecar handshake.")?;
        if handshake.protocol_version != PROTOCOL_VERSION
            || handshake.version.is_empty()
            || !CORE_METHODS.iter().all(|method| handshake.supports(method))
        {
            return Err("The Corinthos sidecar does not support protocol v2.");
        }
        events(LifecycleEvent {
            method: "system.ready",
            params: json!({"protocol_version": PROTOCOL_VERSION}),
        });
        if handshake.supports("session.start") {
            client.shutdown_timeout = SESSION_SHUTDOWN_TIMEOUT;
        }
        Ok((client, handshake))
    }

    pub fn project_status(&mut self, root: &str) -> Result<Value> {
        if root.is_empty() || root.chars().count() > 4096 || root.contains('\0') {
            return Err("Enter a valid project directory.");
        }
        self.request("project.status", json!({"root": root}))
    }

    pub fn providers_status(&mut self) -> Result<Value> {
        self.request("providers.status", json!({}))
    }

    pub fn version(&mut self) -> Result<Value> {
        self.request("system.version", json!({}))
    }

    pub fn is_running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    pub fn shutdown(&mut self) -> Result<()> {
        self.closing.store(true, Ordering::Release);
        let deadline = Instant::now() + self.shutdown_timeout;
        if self.is_running()
            && let Ok(id) = self.send("system.shutdown", json!({}))
        {
            let _ = self.receive_response(id, deadline);
        }
        self.requests.take();
        while self.is_running() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(15));
        }
        self.terminate()
    }

    pub fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        let id = self
            .send(method, params)
            .or_else(|error| self.fail(error))?;
        let timeout = match method {
            "project.open" => PROJECT_OPEN_TIMEOUT,
            "switch.execute" => SWITCH_EXECUTE_TIMEOUT,
            "import.inspect" => IMPORT_INSPECT_TIMEOUT,
            "import.commit" | "import.refresh" => IMPORT_WRITE_TIMEOUT,
            _ => REQUEST_TIMEOUT,
        };
        self.receive_response(id, Instant::now() + timeout)
    }

    fn send(&mut self, method: &str, params: Value) -> Result<u64> {
        let id = self.next_id;
        let mut frame = serde_json::to_vec(&json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }))
        .map_err(|_| "Could not encode the sidecar request.")?;
        if frame.len() > MAX_FRAME_BYTES {
            return Err("The sidecar request exceeds its size limit.");
        }
        frame.push(b'\n');
        self.requests
            .as_ref()
            .ok_or("The Corinthos sidecar has stopped.")?
            .try_send(frame)
            .map_err(|_| "Could not send a request to the Corinthos sidecar.")?;
        self.next_id += 1;
        Ok(id)
    }

    fn receive(&self, deadline: Instant) -> Result<Message> {
        self.messages
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(|error| match error {
                mpsc::RecvTimeoutError::Timeout => "The Corinthos sidecar timed out.",
                mpsc::RecvTimeoutError::Disconnected => "The Corinthos sidecar disconnected.",
            })
    }

    fn receive_response(&mut self, id: u64, deadline: Instant) -> Result<Value> {
        match self.receive(deadline) {
            Ok(Message::Response(received, result)) if received == id => result,
            outcome => self.fail(
                outcome
                    .err()
                    .unwrap_or("The sidecar response did not match its request."),
            ),
        }
    }

    fn fail<T>(&mut self, error: &'static str) -> Result<T> {
        disconnected(&self.closing, &self.events);
        self.requests.take();
        self.terminate()?;
        Err(error)
    }

    fn terminate(&mut self) -> Result<()> {
        if self.is_running() && self.child.kill().is_err() && self.is_running() {
            return Err("Could not terminate the Corinthos sidecar.");
        }
        self.child
            .wait()
            .map_err(|_| "Could not reap the Corinthos sidecar.")?;
        Ok(())
    }
}

fn disconnected(closing: &AtomicBool, events: &EventSink) {
    if !closing.swap(true, Ordering::AcqRel) {
        events(LifecycleEvent {
            method: "backend.disconnected",
            params: json!({}),
        });
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        self.closing.store(true, Ordering::Release);
        self.requests.take();
        let _ = self.terminate();
    }
}

enum Wire {
    Ready,
    Stopping,
    Response(u64, Result<Value>),
}

fn decode(frame: &[u8]) -> Result<Wire> {
    let invalid = "The sidecar returned an invalid protocol frame.";
    let value: Value = serde_json::from_slice(frame).map_err(|_| invalid)?;
    let object = value.as_object().ok_or(invalid)?;
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") || object.len() != 3 {
        return Err(invalid);
    }
    if let Some(id) = object.get("id").and_then(Value::as_u64) {
        if let Some(result) = object.get("result") {
            return Ok(Wire::Response(id, Ok(result.clone())));
        }
        if object
            .get("error")
            .and_then(Value::as_object)
            .is_some_and(|error| {
                error.get("code").and_then(Value::as_i64).is_some()
                    && error.get("message").and_then(Value::as_str).is_some()
            })
        {
            return Ok(Wire::Response(
                id,
                Err("The Corinthos sidecar rejected the request."),
            ));
        }
    }
    match object.get("method").and_then(Value::as_str) {
        // The unnegotiated bootstrap announcement retains the Phase 1 shape.
        Some("system.ready") if object.get("params") == Some(&json!({"protocol_version": 1})) => {
            Ok(Wire::Ready)
        }
        Some("system.stopping") if object.get("params") == Some(&json!({})) => Ok(Wire::Stopping),
        _ => Err(invalid),
    }
}

fn read_frame(reader: &mut impl BufRead) -> io::Result<Option<Vec<u8>>> {
    let mut frame = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(io::ErrorKind::UnexpectedEof.into())
            };
        }
        let delimiter = available.iter().position(|byte| *byte == b'\n');
        let length = delimiter.unwrap_or(available.len());
        if length > MAX_FRAME_BYTES - frame.len() {
            return Err(io::ErrorKind::InvalidData.into());
        }
        frame.extend_from_slice(&available[..length]);
        reader.consume(length + usize::from(delimiter.is_some()));
        if delimiter.is_some() {
            return Ok(Some(frame));
        }
    }
}

fn python_command(executable: &Path, arguments: &[&str]) -> Command {
    let mut command = Command::new(executable);
    command.args(["-I", "-B", "-X", "utf8"]).args(arguments);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    command
}

fn resolve_python() -> Result<PathBuf> {
    if let Some(executable) = env::var_os("CORINTHOS_SIDECAR_PYTHON") {
        let path = PathBuf::from(executable);
        if valid_override(&path) {
            return Ok(path);
        }
        return Err("The contributor Python override must be an existing absolute executable.");
    }
    #[cfg(windows)]
    {
        let path = PathBuf::from(
            env::var_os("LOCALAPPDATA").ok_or("The private Corinthos runtime is unavailable.")?,
        )
        .join("Programs")
        .join("Corinthos")
        .join("runtime")
        .join("python.exe");
        if valid_override(&path) && !has_windows_reparse_point(&path) {
            return Ok(path);
        }
        Err("The private Corinthos runtime is unavailable.")
    }
    #[cfg(not(windows))]
    Err("Set CORINTHOS_SIDECAR_PYTHON to an installed contributor Python runtime.")
}

fn valid_override(path: &Path) -> bool {
    if !path.is_absolute() || !path.is_file() {
        return false;
    }
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        if !matches!(path.components().next(), Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_)))
            || !path
                .extension()
                .is_some_and(|value| value.eq_ignore_ascii_case("exe"))
            || path
                .components()
                .any(|part| matches!(part, Component::ParentDir))
        {
            return false;
        }
    }
    true
}

#[cfg(windows)]
fn has_windows_reparse_point(path: &Path) -> bool {
    use std::os::windows::fs::MetadataExt;
    path.ancestors().any(|part| {
        part.has_root()
            && part.symlink_metadata().map_or(true, |metadata| {
                metadata.file_attributes() & 0x400 != 0 // FILE_ATTRIBUTE_REPARSE_POINT
            })
    })
}

#[cfg(test)]
mod tests;
