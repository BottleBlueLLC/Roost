// Rewritten camera streaming application.
//
// See README.md for the list of bugs fixed relative to the original version.

use ab_glyph::{Font, FontRef, Glyph, PxScale, ScaleFont};
use chrono::Local;
use futures::{SinkExt, StreamExt};
use image::RgbImage;
use serde::{Deserialize, Serialize};
use serde_json::{self, json, Value as JsonValue};
use std::{
    error::Error,
    fs::read_to_string,
    io::Write,
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self as std_mpsc, Receiver as StdReceiver, Sender as StdSender},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tokio::net::TcpListener;
use tokio_tungstenite::{accept_async, tungstenite::protocol::Message, WebSocketStream};
use v4l::{
    buffer::Type, io::traits::CaptureStream, prelude::MmapStream, video::Capture, Device, FourCC,
};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct Config {
    video: VideoConfig,
    network: NetworkConfig,
}

#[derive(Debug, Deserialize)]
struct VideoConfig {
    /// Substring to match against entries in /dev/v4l/by-path to find the
    /// camera. Generic by design -- no hardcoded device keys in main().
    path_identifier: String,
    default_width: u32,
    default_height: u32,
    /// If true, every captured frame is timestamped and written to disk.
    /// If false (default), frames are only saved on snapshot/multisnapshot
    /// requests. Continuous saving at 30fps will wear out an SD card fast,
    /// so this defaults to off.
    #[serde(default)]
    continuous_save: bool,
    #[serde(default = "default_frame_dir")]
    frame_directory: String,
    /// JPEG quality (1-100) used when re-encoding snapshot frames.
    #[serde(default = "default_jpeg_quality")]
    jpeg_quality: i32,
}

fn default_jpeg_quality() -> i32 {
    95
}

fn default_frame_dir() -> String {
    "frames".to_string()
}

#[derive(Debug, Deserialize)]
struct NetworkConfig {
    listener1_address: String,
    listener2_address: String,
}

fn load_config() -> Config {
    let config_str = read_to_string("config.toml").expect("Failed to read config.toml");
    toml::from_str(&config_str).expect("Failed to parse config.toml")
}

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
struct VideoFormat {
    width: u32,
    height: u32,
    fourcc: FourCC,
}

impl VideoFormat {
    fn new(width: u32, height: u32, fourcc: FourCC) -> Self {
        VideoFormat { width, height, fourcc }
    }
}

enum StreamControl {
    Start,
    Stop,
}

#[derive(Clone)]
struct SnapshotRequest;

#[derive(Deserialize, Serialize)]
struct CommandPacket {
    command: String,
    enabled: Option<bool>,
    count: Option<i32>,
    width: Option<i32>,
    height: Option<i32>,
    serial: Option<String>,
    job: Option<String>,
    userid: Option<String>,
}

// ---------------------------------------------------------------------------
// Device discovery (generic -- reads identifier from config, not hardcoded)
// ---------------------------------------------------------------------------

/// Resolves a /dev/videoN path by matching `identifier` against entries in
/// /dev/v4l/by-path. This replaces the old code's hardcoded PCIe path keys
/// in main() -- the identifier is supplied entirely from config.toml.
fn resolve_device_path(identifier: &str) -> Result<String, String> {
    let output = Command::new("ls")
        .arg("-la")
        .arg("/dev/v4l/by-path")
        .output()
        .map_err(|e| format!("Failed to list /dev/v4l/by-path: {e}"))?;

    if !output.status.success() {
        return Err("ls /dev/v4l/by-path failed".to_string());
    }

    let output_str = String::from_utf8_lossy(&output.stdout);
    for line in output_str.lines() {
        if line.contains(identifier) {
            if let Some(target) = line.split("-> ").nth(1) {
                let video_node = target.trim().rsplit('/').next().unwrap_or(target.trim());
                return Ok(format!("/dev/{}", video_node));
            }
        }
    }

    Err(format!(
        "No device found in /dev/v4l/by-path matching identifier '{identifier}'"
    ))
}

// ---------------------------------------------------------------------------
// Image processing helpers
// ---------------------------------------------------------------------------

/// Draws white text in the bottom-right corner of an RGB image by
/// rasterizing glyph outlines directly with ab_glyph. This replaces the
/// original imageproc-based text drawing: imageproc forces the `image`
/// crate's default features on (which pull in an AVIF encoder dependency
/// chain), so we draw glyphs by hand here to keep the dependency tree free
/// of that.
fn draw_text_on_image(image: &mut RgbImage, text: &str) {
    let font = FontRef::try_from_slice(include_bytes!("DejaVuSans.ttf")).unwrap();
    let scale = PxScale::from(22.0);
    let scaled_font = font.as_scaled(scale);
    let (img_w, img_h) = image.dimensions();

    // First pass: compute total rendered width so we can right-align.
    let mut total_width = 0.0f32;
    let mut last_glyph_id = None;
    for c in text.chars() {
        let glyph_id = font.glyph_id(c);
        if let Some(last) = last_glyph_id {
            total_width += scaled_font.kern(last, glyph_id);
        }
        total_width += scaled_font.h_advance(glyph_id);
        last_glyph_id = Some(glyph_id);
    }

    let margin = 8.0;
    let mut caret_x = (img_w as f32 - total_width - margin).max(0.0);
    let baseline_y = img_h as f32 - margin;
    last_glyph_id = None;

    for c in text.chars() {
        let glyph_id = font.glyph_id(c);
        if let Some(last) = last_glyph_id {
            caret_x += scaled_font.kern(last, glyph_id);
        }

        let glyph: Glyph = glyph_id.with_scale_and_position(scale, ab_glyph::point(caret_x, baseline_y));
        if let Some(outlined) = font.outline_glyph(glyph) {
            let bounds = outlined.px_bounds();
            outlined.draw(|gx, gy, coverage| {
                if coverage <= 0.0 {
                    return;
                }
                let px = bounds.min.x as i32 + gx as i32;
                let py = bounds.min.y as i32 + gy as i32;
                if px < 0 || py < 0 || px as u32 >= img_w || py as u32 >= img_h {
                    return;
                }
                let add = (255.0 * coverage) as u8;
                let existing = image.get_pixel(px as u32, py as u32).0;
                let blended = image::Rgb([
                    existing[0].saturating_add(add),
                    existing[1].saturating_add(add),
                    existing[2].saturating_add(add),
                ]);
                image.put_pixel(px as u32, py as u32, blended);
            });
        }

        caret_x += scaled_font.h_advance(glyph_id);
        last_glyph_id = Some(glyph_id);
    }
}

fn decompress_jpeg_to_rgb(buffer: &[u8]) -> Result<RgbImage, Box<dyn Error>> {
    let mut decompressor = turbojpeg::Decompressor::new()?;
    let header = decompressor.read_header(buffer)?;
    let mut image = turbojpeg::Image {
        pixels: vec![0u8; header.width * header.height * 3],
        width: header.width,
        pitch: header.width * 3,
        height: header.height,
        format: turbojpeg::PixelFormat::RGB,
    };
    decompressor.decompress(buffer, image.as_deref_mut())?;
    RgbImage::from_raw(header.width as u32, header.height as u32, image.pixels)
        .ok_or_else(|| "failed to construct RgbImage from decoded buffer".into())
}

fn compress_rgb_to_jpeg(img: &RgbImage, quality: i32) -> Result<Vec<u8>, Box<dyn Error>> {
    let mut compressor = turbojpeg::Compressor::new()?;
    compressor.set_quality(quality);
    compressor.set_subsamp(turbojpeg::Subsamp::Sub2x2);

    let (width, height) = img.dimensions();
    let image = turbojpeg::Image {
        pixels: img.as_raw().as_slice(),
        width: width as usize,
        pitch: width as usize * 3,
        height: height as usize,
        format: turbojpeg::PixelFormat::RGB,
    };
    let owned_buf = compressor.compress_to_vec(image)?;
    Ok(owned_buf.to_vec())
}

fn add_timestamp_to_image(buffer: &[u8], quality: i32) -> Result<Vec<u8>, Box<dyn Error>> {
    let now = Local::now();
    let timestamp = now.format("%Y-%m-%d %H:%M:%S").to_string();

    let mut rgb_image = decompress_jpeg_to_rgb(buffer)?;
    draw_text_on_image(&mut rgb_image, &timestamp);

    compress_rgb_to_jpeg(&rgb_image, quality)
}

/// Dedicated thread that writes frames to disk, fed by a channel. Avoids
/// spawning a new OS thread per frame (the old code spawned a thread for
/// every single captured frame).
///
/// Unlike the original `save_images_in_order`, this does not buffer frames
/// waiting for a strict sequential order: frames arrive from a single
/// producer thread (`capture_worker`) over one mpsc channel, which already
/// guarantees in-order delivery, so there's no out-of-order case to handle.
/// (Re-ordering by frame number was actually a bug here: with
/// `continuous_save` off, only snapshot-triggered frames are sent, so frame
/// numbers are sparse -- e.g. frame #47 might be the first one sent. Waiting
/// for #0, #1, #2... first meant nothing was ever written.)
fn frame_writer_thread(rx: StdReceiver<(u64, String, Vec<u8>)>, frame_dir: String) {
    let _ = std::fs::create_dir_all(&frame_dir);

    while let Ok((frame_number, label, data)) = rx.recv() {
        let file_name = format!("{}/{}_frame_{}.jpg", frame_dir, label, frame_number);
        match std::fs::write(&file_name, &data) {
            Ok(_) => println!("Saved {}", file_name),
            Err(e) => eprintln!("Failed to save {}: {}", file_name, e),
        }
    }
}

// ---------------------------------------------------------------------------
// Capture worker
// ---------------------------------------------------------------------------

/// Owns the v4l Device and Stream entirely within this function's scope, so
/// there is no cross-function lifetime to manage (this is what caused the
/// original `Stream<'a>` lifetime error -- a Device dropped at the end of an
/// `initialize_video_stream()` call while a Stream borrowing from it was
/// returned to the caller).
///
/// Runs on a blocking thread (via spawn_blocking from main) since
/// `Stream::next()` is a blocking syscall and must never run directly on a
/// Tokio worker thread.
fn capture_worker(
    label: String,
    device_path: String,
    format: Arc<Mutex<VideoFormat>>,
    control_rx: StdReceiver<StreamControl>,
    snapshot_rx: StdReceiver<SnapshotRequest>,
    frame_tx: StdSender<(u64, String, Vec<u8>)>,
    continuous_save: bool,
    jpeg_quality: i32,
) {
    let mut is_active = true;
    let mut global_frame_count: u64 = 0;

    'restart: loop {
        let active_format = *format.lock().unwrap();

        let dev = match Device::with_path(&device_path) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("{}: failed to open {}: {}", label, device_path, e);
                thread::sleep(Duration::from_secs(2));
                continue 'restart;
            }
        };

        let mut fmt = match dev.format() {
            Ok(f) => f,
            Err(e) => {
                eprintln!("{}: failed to read format: {}", label, e);
                thread::sleep(Duration::from_secs(2));
                continue 'restart;
            }
        };
        fmt.width = active_format.width;
        fmt.height = active_format.height;
        fmt.fourcc = active_format.fourcc;

        if let Err(e) = dev.set_format(&fmt) {
            eprintln!("{}: failed to set format: {}", label, e);
            thread::sleep(Duration::from_secs(2));
            continue 'restart;
        }

        let mut stream = match MmapStream::new(&dev, Type::VideoCapture) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("{}: failed to create stream: {}", label, e);
                thread::sleep(Duration::from_secs(2));
                continue 'restart;
            }
        };

        println!(
            "{}: streaming {}x{} ({}) from {}",
            label, active_format.width, active_format.height, device_path, device_path
        );

        let start_time = Instant::now();
        let mut frames_this_run = 0u64;

        loop {
            // Resolution change requested -- tear down and reopen with the
            // new format. This is the fix for the old `videoresolution`
            // command, which updated the shared format struct but the
            // running stream never re-read it.
            if *format.lock().unwrap() != active_format {
                println!("{}: format change detected, restarting stream", label);
                continue 'restart;
            }

            if let Ok(msg) = control_rx.try_recv() {
                match msg {
                    StreamControl::Start => is_active = true,
                    StreamControl::Stop => is_active = false,
                }
            }

            if !is_active {
                thread::sleep(Duration::from_millis(50));
                continue;
            }

            match stream.next() {
                Ok((buf, _meta)) => {
                    let want_snapshot = snapshot_rx.try_recv().is_ok();

                    if continuous_save || want_snapshot {
                        match add_timestamp_to_image(buf, jpeg_quality) {
                            Ok(modified) => {
                                if frame_tx
                                    .send((global_frame_count, label.clone(), modified))
                                    .is_err()
                                {
                                    eprintln!("{}: frame writer channel closed", label);
                                }
                            }
                            Err(e) => eprintln!("{}: failed to timestamp frame: {}", label, e),
                        }
                    }

                    global_frame_count += 1;
                    frames_this_run += 1;

                    if frames_this_run % 100 == 0 {
                        let elapsed = start_time.elapsed().as_secs_f32();
                        let fps = frames_this_run as f32 / elapsed;
                        println!(
                            "{}: {} frames in {:.2}s, {:.2} fps",
                            label, frames_this_run, elapsed, fps
                        );
                    }
                }
                Err(e) => {
                    eprintln!("{}: stream read error: {}, restarting", label, e);
                    continue 'restart;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Printer helpers (unchanged behavior, just no #![allow(warnings)] hiding
// issues anymore)
// ---------------------------------------------------------------------------

async fn check_for_printers(enable_printers: bool) -> JsonValue {
    if !enable_printers {
        return JsonValue::Array(vec![]);
    }
    println!("Checking for printers...");
    let output = Command::new("./scripts/printer_enum.sh")
        .arg("enum")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output();

    match output {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout);
            serde_json::from_str(&stdout).unwrap_or_else(|_| JsonValue::Array(vec![]))
        }
        Err(e) => {
            println!("Error executing printer enum command: {:?}", e);
            JsonValue::Array(vec![])
        }
    }
}

fn print_job(packet: &CommandPacket) {
    let job_length = packet.job.as_ref().map_or(0, |job| job.len() / 2);
    let default_serial = String::from("unknown");
    let serial = packet.serial.as_ref().unwrap_or(&default_serial);
    println!("Printing {} bytes to {}...", job_length, serial);

    let Some(job_data) = packet.job.as_ref() else {
        println!("No job data to print.");
        return;
    };

    let output = Command::new("./scripts/printer_helper.sh")
        .arg("print")
        .arg(serial)
        .arg(job_data)
        .stderr(Stdio::null())
        .output();

    match output {
        Ok(output) => println!("print job resulted in {} bytes output", output.stdout.len()),
        Err(e) => println!("Error executing print command: {:?}", e),
    }
}

fn update_current_user_id(id: &str) -> std::io::Result<()> {
    let mut file = std::fs::File::create("/tmp/product_current_user_id_camera")?;
    file.write_all(id.as_bytes())?;
    file.flush()?;
    Command::new("sudo")
        .arg("mv")
        .arg("/tmp/product_current_user_id_camera")
        .arg("/tmp/product_current_user_id")
        .status()?;
    Ok(())
}

// ---------------------------------------------------------------------------
// WebSocket: control socket (port 8080) -- snapshot / resolution / printer
// ---------------------------------------------------------------------------

async fn send_to_control(ws_stream: &mut WebSocketStream<tokio::net::TcpStream>, command: &str, message: &str) {
    let control_message = json!({ "command": command, "message": message }).to_string();
    if let Err(e) = ws_stream.send(Message::Text(control_message)).await {
        eprintln!("Error sending to control: {:?}", e);
    }
}

async fn handle_camera_control_socket(
    mut ws_stream: WebSocketStream<tokio::net::TcpStream>,
    snapshot_tx: StdSender<SnapshotRequest>,
    running: Arc<AtomicBool>,
    format: Arc<Mutex<VideoFormat>>,
) {
    loop {
        // tokio::select! lets us handle incoming websocket messages and poll
        // the sensor-alert file concurrently, without ever blocking the
        // runtime. The old code polled this file with a brand-new
        // `Runtime::new().block_on(...)` nested inside an already-running
        // async task -- that's a real deadlock/stall risk and is gone here.
        tokio::select! {
            message_result = ws_stream.next() => {
                let Some(message_result) = message_result else { break };
                match message_result {
                    Ok(Message::Text(text)) => {
                        println!("Received message: {:?}", text);
                        let Ok(packet) = serde_json::from_str::<CommandPacket>(&text) else {
                            eprintln!("Failed to parse command from message");
                            continue;
                        };
                        match packet.command.as_str() {
                            "quit" => break,
                            "snapshot" => {
                                println!("Snapshot command!");
                                let _ = snapshot_tx.send(SnapshotRequest);
                            }
                            "shutdown" => {
                                println!("Shutdown command!");
                                running.store(false, Ordering::SeqCst);
                                break;
                            }
                            "multisnapshot" => {
                                let count = packet.count.unwrap_or(15);
                                println!("Multisnapshot command! ({} frames)", count);
                                for _ in 0..count {
                                    let _ = snapshot_tx.send(SnapshotRequest);
                                }
                            }
                            "videoresolution" => {
                                let width = packet.width.unwrap_or(1920) as u32;
                                let height = packet.height.unwrap_or(1080) as u32;
                                println!("Video resolution command received! {}x{}", width, height);
                                let mut current = format.lock().unwrap();
                                *current = VideoFormat::new(width, height, FourCC::new(b"MJPG"));
                            }
                            "printquery" => {
                                let printers = check_for_printers(true).await;
                                let data = json!({ "command": "printquery", "printers": printers });
                                if let Err(e) = ws_stream.send(Message::Text(data.to_string())).await {
                                    eprintln!("Error sending printer query response: {:?}", e);
                                }
                            }
                            "printjob" => {
                                println!("Print job command received!");
                                print_job(&packet);
                            }
                            "impact_sensor_alarm" => {
                                println!("impact_sensor_alarm command!");
                                for _ in 0..15 {
                                    let _ = snapshot_tx.send(SnapshotRequest);
                                }
                            }
                            "useridset" => {
                                if let Some(user_id) = packet.userid.as_deref() {
                                    if let Err(e) = update_current_user_id(user_id) {
                                        eprintln!("Failed to update user id: {:?}", e);
                                    }
                                } else {
                                    println!("Userid is none");
                                }
                            }
                            _ => println!("Unknown command"),
                        }
                    }
                    Ok(_) => {} // ignore non-text frames (ping/pong/binary/close)
                    Err(e) => {
                        eprintln!("Error in WebSocket stream: {:?}", e);
                        break;
                    }
                }
            }

            _ = tokio::time::sleep(Duration::from_millis(500)) => {
                let file_path = "/tmp/sensor_alert";
                if let Ok(message) = tokio::fs::read_to_string(file_path).await {
                    send_to_control(&mut ws_stream, "impact_sensor_alarm", "The alarm message").await;
                    for _ in 0..15 {
                        let _ = snapshot_tx.send(SnapshotRequest);
                    }
                    if let Err(e) = tokio::fs::remove_file(file_path).await {
                        eprintln!("Failed to remove sensor alert file: {:?}", e);
                    }
                    println!("Processed alarm with message: {}", message);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// WebSocket: stream control socket (port 8081) -- start/stop
// ---------------------------------------------------------------------------

async fn handle_command_control_socket(
    mut ws_stream: WebSocketStream<tokio::net::TcpStream>,
    control_tx: StdSender<StreamControl>,
) {
    while let Some(message_result) = ws_stream.next().await {
        match message_result {
            Ok(Message::Text(text)) => {
                println!("Received message: {:?}", text);
                let Ok(packet) = serde_json::from_str::<CommandPacket>(&text) else {
                    eprintln!("Failed to parse command from message");
                    continue;
                };
                match packet.command.as_str() {
                    "quit" => break,
                    "start_camera" => {
                        println!("Start camera command");
                        let _ = control_tx.send(StreamControl::Start);
                    }
                    "stop_camera" => {
                        println!("Stop camera command");
                        let _ = control_tx.send(StreamControl::Stop);
                    }
                    _ => println!("Unknown command"),
                }
            }
            Ok(_) => {}
            Err(e) => {
                eprintln!("Error in WebSocket stream: {:?}", e);
                break;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    let config = load_config();

    let device_path = match resolve_device_path(&config.video.path_identifier) {
        Ok(path) => path,
        Err(e) => {
            eprintln!("{e}");
            eprintln!("Falling back to /dev/video0");
            "/dev/video0".to_string()
        }
    };
    println!("Using camera device: {}", device_path);

    let format = Arc::new(Mutex::new(VideoFormat::new(
        config.video.default_width,
        config.video.default_height,
        FourCC::new(b"MJPG"),
    )));

    let (control_tx, control_rx) = std_mpsc::channel::<StreamControl>();
    let (snapshot_tx, snapshot_rx) = std_mpsc::channel::<SnapshotRequest>();
    let (frame_tx, frame_rx) = std_mpsc::channel::<(u64, String, Vec<u8>)>();

    let frame_dir = config.video.frame_directory.clone();
    thread::spawn(move || frame_writer_thread(frame_rx, frame_dir));

    let format_for_worker = format.clone();
    let continuous_save = config.video.continuous_save;
    let jpeg_quality = config.video.jpeg_quality;
    let worker_label = "camera".to_string();
    let worker_device_path = device_path.clone();
    tokio::task::spawn_blocking(move || {
        capture_worker(
            worker_label,
            worker_device_path,
            format_for_worker,
            control_rx,
            snapshot_rx,
            frame_tx,
            continuous_save,
            jpeg_quality,
        );
    });

    let running = Arc::new(AtomicBool::new(true));

    let listener = TcpListener::bind(&config.network.listener1_address)
        .await
        .expect("Failed to bind to address for listener 1");
    let listener2 = TcpListener::bind(&config.network.listener2_address)
        .await
        .expect("Failed to bind to address for listener 2");

    println!("Camera control socket listening on {}", config.network.listener1_address);
    println!("Stream control socket listening on {}", config.network.listener2_address);

    while running.load(Ordering::SeqCst) {
        tokio::select! {
            Ok((stream, _)) = listener.accept() => {
                let snapshot_tx_clone = snapshot_tx.clone();
                let running_clone = running.clone();
                let format_clone = format.clone();
                match accept_async(stream).await {
                    Ok(ws_stream) => {
                        tokio::spawn(handle_camera_control_socket(
                            ws_stream,
                            snapshot_tx_clone,
                            running_clone,
                            format_clone,
                        ));
                    }
                    Err(e) => eprintln!("Failed to accept websocket on listener 1: {:?}", e),
                }
            },
            Ok((stream, _)) = listener2.accept() => {
                let control_tx_clone = control_tx.clone();
                match accept_async(stream).await {
                    Ok(ws_stream) => {
                        tokio::spawn(handle_command_control_socket(ws_stream, control_tx_clone));
                    }
                    Err(e) => eprintln!("Failed to accept websocket on listener 2: {:?}", e),
                }
            },
            _ = tokio::time::sleep(Duration::from_millis(500)) => {
                if !running.load(Ordering::SeqCst) {
                    break;
                }
            }
        }
    }

    println!("Exiting...");
}
