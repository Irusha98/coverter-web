# app.py
# app.py
import sys
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from flask import Flask, render_template, request, redirect, url_for, send_file, abort, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import os
import io
import base64
import traceback
import tempfile
import json
import shutil
import uuid
from flask import request, redirect
from flask import send_from_directory

# --- Core MoviePy Imports (for trim/merge only, .fx() avoided) ---
VideoFileClip = None
AudioFileClip = None
concatenate_videoclips = None
CompositeAudioClip = None
afx = None
vfx = None

try:
    from moviepy.editor import VideoFileClip, concatenate_videoclips, vfx, AudioFileClip, CompositeAudioClip, afx
    print("moviepy.editor core components imported successfully.")
except ImportError as e:
    print(f"CRITICAL ERROR: Could not import core moviepy.editor components: {e}", file=sys.stderr)
    print("Video editing features (trim, merge) might be limited or unavailable.", file=sys.stderr)
    print("Please ensure moviepy is installed: pip install moviepy", file=sys.stderr)
except Exception as e:
    print(f"An unexpected error occurred during moviepy.editor import: {e}", file=sys.stderr)

# --- FFmpeg and Pydub Imports ---
from pydub import AudioSegment
from pydub.utils import which # For finding ffmpeg

_ffmpeg_available = False
try:
    ffmpeg_path = which("ffmpeg")
    if ffmpeg_path:
        AudioSegment.converter = ffmpeg_path
        os.environ["FFMPEG_BINARY"] = ffmpeg_path # Set for pydub
        os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path # Set for moviepy (if used)
        _ffmpeg_available = True
        print(f"FFmpeg found at: {ffmpeg_path}")
    else:
        print("FFmpeg not found. Audio/Video processing will not work.", file=sys.stderr)
        print("Please install FFmpeg and ensure it's in your system's PATH.", file=sys.stderr)
except Exception as e:
    print(f"Error setting up FFmpeg: {e}", file=sys.stderr)


import fitz # PyMuPDF
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.colors import HexColor # Needed for PDF text coloring
from PIL import Image # Pillow library
import subprocess # Essential for running external commands like unoconv
from pdf2docx import Converter # For PDF to Word conversion

# NEW IMPORT for CairoSVG
try:
    import cairosvg
    print("cairosvg imported successfully.")
except ImportError:
    print("cairosvg not found. SVG processing might be limited or unavailable.", file=sys.stderr)
    print("Please install cairosvg: pip install cairosvg", file=sys.stderr)
    cairosvg = None # Set to None if import fails, to handle gracefully
except Exception as e:
    print(f"An unexpected error occurred during cairosvg import: {e}", file=sys.stderr)


# NEW IMPORT for archive extraction
try:
    import patoolib
    print("patoolib imported successfully.")
except ImportError:
    print("patoolib not found. Archive extraction features will not work.", file=sys.stderr)
    print("Please install patool: pip install patool", file=sys.stderr)
    patoolib = None


# Image.MAX_IMAGE_PIXELS = None # Uncomment this if you suspect memory issues with large images


app = Flask(__name__)

# Upload folder configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Define specific folders for different output types
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
CONVERTED_FOLDER = os.path.join(BASE_DIR, 'converted')
MERGED_FOLDER = os.path.join(BASE_DIR, 'merged')
TRIMMED_FOLDER = os.path.join(BASE_DIR, 'trimmed')
EXTRACTED_FOLDER = os.path.join(BASE_DIR, 'extracted_archives')
CREATED_ARCHIVES_FOLDER = os.path.join(BASE_DIR, 'created_archives')
SECURE_PDF_FOLDER = os.path.join(BASE_DIR, 'secure_pdfs')

# Using a temp directory for processed videos, as they can be large and temporary
PROCESSED_VIDEOS_TEMP_DIR = os.path.join(tempfile.gettempdir(), "processed_videos_temp")

# NEW: Directory for temporarily storing processed audio files before explicit download
PROCESSED_AUDIO_TEMP_DIR = os.path.join(tempfile.gettempdir(), "processed_audio_temp")


# Register all folders in app.config
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['CONVERTED_FOLDER'] = CONVERTED_FOLDER
app.config['MERGED_FOLDER'] = MERGED_FOLDER
app.config['TRIMMED_FOLDER'] = TRIMMED_FOLDER
app.config['EXTRACTED_FOLDER'] = EXTRACTED_FOLDER
app.config['CREATED_ARCHIVES_FOLDER'] = CREATED_ARCHIVES_FOLDER
app.config['SECURE_PDF_FOLDER'] = SECURE_PDF_FOLDER
app.config['PROCESSED_VIDEOS_TEMP_DIR'] = PROCESSED_VIDEOS_TEMP_DIR # Register for video output
app.config['PROCESSED_AUDIO_TEMP_DIR'] = PROCESSED_AUDIO_TEMP_DIR # Register for audio output


app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 # Increased to 500 MB for video files

# --- Global empirical offsets for PDF rendering alignment (in PDF points) ---
# These values compensate for subtle differences between browser rendering and PDF rendering.
# Adjust these values based on observed shifts in downloaded PDF.
# Positive values shift elements further down/right in the FINAL PDF. Negative shift up/left.
# These values were refined during PDF debugging.
PDF_RENDER_ADJUSTMENT_X_BACKEND = -2.5  # Shift content left by 2.5 PDF points
PDF_RENDER_ADJUSTMENT_Y_BACKEND = -6.0  # Shift content up by 6.0 PDF points

# Set the UNO_PATH environment variable for unoconv on macOS
# This tells unoconv where to find the LibreOffice installation.
# IMPORTANT: Verify this path matches your LibreOffice installation on macOS.
# Typical path is /Applications/LibreOffice.app/Contents/Resources
if sys.platform == "darwin": # Check if running on macOS
    # Default common path for LibreOffice on macOS
    libreoffice_app_path = "/Applications/LibreOffice.app"
    # Alternative for LibreOffice Fresh if that's installed
    if not os.path.exists(libreoffice_app_path):
        libreoffice_app_path = "/Applications/LibreOffice Fresh.app"

    uno_path = os.path.join(libreoffice_app_path, "Contents", "Resources")
    if os.path.exists(uno_path):
        os.environ["UNO_PATH"] = uno_path
        print(f"Set UNO_PATH to: {os.environ['UNO_PATH']}")
    else:
        print(f"WARNING: LibreOffice Resources not found at {uno_path}. "
              "Word to PDF conversion might fail if LibreOffice is not found by unoconv. "
              "Please ensure LibreOffice is installed and its UNO_PATH is correctly set.", file=sys.stderr)


# --- FIX START: Ensure all necessary directories exist when the app starts ---
# This block is placed here so it runs when the Flask application is loaded by the web server.
try:
    os.makedirs(app.config['PROCESSED_VIDEOS_TEMP_DIR'], exist_ok=True)
    os.makedirs(app.config['PROCESSED_AUDIO_TEMP_DIR'], exist_ok=True)
    for folder in [
        app.config['UPLOAD_FOLDER'],
        app.config['CONVERTED_FOLDER'],
        app.config['MERGED_FOLDER'],
        app.config['TRIMMED_FOLDER'],
        app.config['EXTRACTED_FOLDER'],
        app.config['CREATED_ARCHIVES_FOLDER'],
        app.config['SECURE_PDF_FOLDER']
    ]:
        os.makedirs(folder, exist_ok=True)
    print("All necessary directories ensured to exist.")
except Exception as e:
    print(f"CRITICAL ERROR: Failed to create necessary directories: {e}", file=sys.stderr)
    # Depending on the severity, you might want to raise the exception or exit here.
    # For a web app, it's often better to log and let it potentially crash so it gets noticed.
# --- FIX END ---


# --- Helper to determine file type based on mimetype (less critical if handled by frontend input `accept` attributes) ---
def get_file_type(mimetype):
    """Checks the MIME type and returns a simplified file type category."""
    if mimetype:
        if mimetype.startswith('audio/'):
            return 'audio'
        if mimetype.startswith('video/'):
            return 'video'
        if mimetype.startswith('image/'):
            return 'image'
        if 'pdf' in mimetype: # check for 'pdf' substring in case of generic mimetype like 'application/octet-stream'
            return 'pdf'
        if 'word' in mimetype or 'document' in mimetype:
            return 'word'
        if 'zip' in mimetype or 'rar' in mimetype or '7z' in mimetype:
            return 'archive'
    return 'unknown'



# --- Helper to check allowed file extensions ---
def allowed_file(filename):
    """Checks if the file extension is allowed based on app.config['ALLOWED_EXTENSIONS']."""
    # This function is currently not used with a global ALLOWED_EXTENSIONS list,
    # as validation is done per-route.
    return '.' in filename


# --- Routes ---

@app.route('/')
def home():
    """Renders the main landing page (index.html)."""
    return render_template('index.html')

def force_https_in_production():
    if request.headers.get('X-Forwarded-Proto', 'http') != 'https':
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

# This is the correct and only definition for the /about route
@app.route('/about')
def about():
    """Renders the About Us page."""
    return render_template('About.html')

@app.route('/ai')
def ai_page():
    """Renders the AI.html page."""
    return render_template('AI.html')

@app.route('/contact')
def contact_page():
    """Renders the conatct.html page."""
    return render_template('contact.html')

@app.route('/privacy-policy') # NEW ROUTE FOR PRIVACY POLICY
def privacy_policy():
    """Renders the privacy.html page."""
    return render_template('privacy.html')

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('static', 'sitemap.xml')

@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt')


@app.route('/unified-video-editor')
def unified_video_editor_page():
    """Renders the unified video editor HTML page."""
    return render_template('unified_video_editor.html')

@app.route('/split-video', methods=['POST'])
def split_video():
    """Handles video splitting (trimming) functionality."""
    try:
        video = request.files['video_file']
        start = float(request.form['start_time'])
        end = float(request.form['end_time'])
        output_format = request.form['output_format']

        if not VideoFileClip:
            return jsonify(error="Video trimming is not available. MoviePy VideoFileClip not loaded."), 500

        # Save to a temporary file first
        temp_input_filename = secure_filename(video.filename)
        temp_input_filepath = os.path.join(app.config['UPLOAD_FOLDER'], temp_input_filename)
        video.save(temp_input_filepath)

        clip = VideoFileClip(temp_input_filepath)
        # subclip does not use .fx(), so it should be fine
        subclip = clip.subclip(start, end)

        filename = f"{uuid.uuid4()}.{output_format}"
        # Use PROCESSED_VIDEOS_TEMP_DIR from app.config
        output_path = os.path.join(app.config['PROCESSED_VIDEOS_TEMP_DIR'], filename)
        subclip.write_videofile(output_path, codec='libx264', audio_codec='aac')

        clip.close() # Close the original clip
        subclip.close() # Close the subclip

        return jsonify({"success": True, "download_url": f"/download-video/{filename}?folder=trimmed_videos"})
    except Exception as e:
        print(f"Error during video splitting: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"success": False, "error": str(e)})
    finally:
        if 'temp_input_filepath' in locals() and os.path.exists(temp_input_filepath):
            try: os.remove(temp_input_filepath)
            except OSError as e: print(f"Error removing temp_input_filepath {temp_input_filepath}: {e}", file=sys.stderr)


@app.route('/merge-video', methods=['POST'])
def merge_video():
    """Handles video merging (concatenation) functionality."""
    if not concatenate_videoclips:
        return jsonify(error="Video merging is not available. MoviePy concatenate_videoclips not loaded."), 500

    video_files_storage = request.files.getlist('video_files')
    output_format = request.form.get('output_format', 'mp4')

    if not video_files_storage or len(video_files_storage) < 2:
        return jsonify(error="Please provide at least two video files for merging."), 400

    # Validate file types for all uploaded files
    for file_storage in video_files_storage:
        if not file_storage.filename.lower().endswith(('.mp4', '.mov', '.avi', '.webm', '.mkv')):
            return jsonify(error="Invalid file type for video merging. Only common video formats are supported."), 400

    allowed_formats = {'mp4': 'video/mp4', 'mov': 'video/quicktime', 'webm': 'video/webm'}
    if output_format not in allowed_formats:
        return jsonify(error=f"Unsupported output format: {output_format}"), 400

    input_paths = []
    clips = []
    try:
        for file_storage in video_files_storage:
            original_filename = secure_filename(file_storage.filename)
            input_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
            file_storage.save(input_path)
            input_paths.append(input_path)
            clips.append(VideoFileClip(input_path))

        output_filename = f"merged_video_{uuid.uuid4()}.{output_format}" # Unique name for output
        # Use PROCESSED_VIDEOS_TEMP_DIR from app.config
        output_path = os.path.join(app.config['PROCESSED_VIDEOS_TEMP_DIR'], output_filename)

        # concatenate_videoclips does not use .fx()
        final_clip = concatenate_videoclips(clips)

        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac",
                                    temp_audiofile=f"{output_path}_temp_audio.m4a", remove_temp=True)

        # Close all clips and clean up input files
        for clip in clips:
            clip.close()
        final_clip.close()


        print(f"Videos concatenated successfully. Sending file: {output_path}")
        return jsonify(success=True, download_url=f"/download-video/{output_filename}?folder=merged_videos")

    except Exception as e:
        print(f"Error during video concatenation: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"Error during video concatenation: {e}"), 500
    finally:
        for path in input_paths:
            if os.path.exists(path):
                try: os.remove(path)
                except OSError as e: print(f"Error removing input file {path}: {e}", file=sys.stderr)


@app.route('/video-speed-changer', methods=['GET'])
def video_speed_changer_page():
    """Renders the video speed changer HTML page."""
    return render_template('unified_video_editor.html')

@app.route('/process-speed-change-video', methods=['POST'])
def process_speed_change_video():
    """Handles changing video playback speed using FFmpeg."""
    if not _ffmpeg_available:
        return jsonify(error="FFmpeg is not found. Video speed change is not available. Please install FFmpeg."), 500

    video_file_storage = request.files.get('video_file')
    speed_factor = float(request.form.get('speed_factor', 1.0))
    output_format = request.form.get('output_format', 'mp4')

    if not video_file_storage:
        return jsonify(error="No video file provided for speed change."), 400

    if not video_file_storage.filename.lower().endswith(('.mp4', '.mov', '.avi', '.webm', '.mkv')):
        return jsonify(error="Invalid file type for video speed change. Only common video formats are supported."), 400

    if speed_factor <= 0:
        return jsonify(error="Speed factor must be greater than 0."), 400

    allowed_formats = {'mp4': 'video/mp4', 'mov': 'video/quicktime', 'webm': 'video/webm'}
    if output_format not in allowed_formats:
        return jsonify(error=f"Unsupported output format: {output_format}"), 400

    original_filename = secure_filename(video_file_storage.filename)
    temp_input_file_name = None

    try:
        # Save the uploaded file to a temporary location
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(original_filename)[1]) as temp_input_file:
            video_file_storage.save(temp_input_file.name)
            temp_input_file_name = temp_input_file.name

        base_filename_no_ext = os.path.splitext(original_filename)[0]
        output_filename = f"speed_{speed_factor}x_{base_filename_no_ext}_{uuid.uuid4()}.{output_format}" # Unique output name
        # Use PROCESSED_VIDEOS_TEMP_DIR from app.config
        output_path = os.path.join(app.config['PROCESSED_VIDEOS_TEMP_DIR'], output_filename)

        # FFmpeg filters for speed change:
        # setpts=(1/speed_factor)*PTS : Changes video playback speed
        # atempo=speed_factor : Changes audio playback speed (only works for 0.5x to 2.0x)
        # If speed_factor is outside 0.5-2.0, multiple atempo filters can be chained

        # Calculate speed for atempo filter (clamped between 0.5 and 2.0)
        atempo_factor = speed_factor
        atempo_filters = []

        # Chain atempo filters if speed_factor is outside the 0.5x to 2.0x range
        # This loop repeatedly applies atempo=2.0 until atempo_factor is <= 2.0
        while atempo_factor > 2.0:
            atempo_filters.append("atempo=2.2") # Use 2.2 as 2.0 can cause issues with some ffmpeg versions. Max is usually 2.0 or 2.2
            atempo_factor /= 2.2
        # This loop repeatedly applies atempo=0.5 until atempo_factor is >= 0.5
        while atempo_factor < 0.5:
            atempo_filters.append("atempo=0.5")
            atempo_factor /= 0.5

        # Add the remaining atempo factor (which will now be between 0.5 and 2.0)
        if atempo_factor != 1.0: # Only add if there's an actual change
            atempo_filters.append(f"atempo={atempo_factor}")

        audio_filter_string = ",".join(atempo_filters) if atempo_filters else "anull" # anull is a no-op audio filter

        ffmpeg_command = [
            ffmpeg_path,
            '-i', temp_input_file_name,
            '-filter_complex', f"[0:v]setpts={1/speed_factor}*PTS[v];[0:a]{audio_filter_string}[a]",
            '-map', '[v]', '-map', '[a]',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', # Video codec settings
            '-c:a', 'aac', '-b:a', '128k', # Audio codec settings
            output_path
        ]

        print(f"FFmpeg command for speed change: {' '.join(ffmpeg_command)}")

        process = subprocess.run(ffmpeg_command, capture_output=True, text=True, check=False)

        if process.returncode != 0:
            print(f"FFmpeg Error (return code {process.returncode}):", file=sys.stderr)
            print("FFmpeg STDOUT:", process.stdout, file=sys.stderr)
            print("FFmpeg STDERR:", process.stderr, file=sys.stderr)
            raise Exception(f"FFmpeg processing failed: {process.stderr}")

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("FFmpeg did not produce a valid output file.")

        print(f"Video speed changed successfully. Sending file: {output_path}")
        return jsonify(success=True, download_url=f"/download-video/{os.path.basename(output_path)}?folder=trimmed_videos")

    except Exception as e:
        print(f"Error during video speed change: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"An internal server error occurred during video speed change: {e}"), 500
    finally:
        if temp_input_file_name and os.path.exists(temp_input_file_name):
            try:
                os.remove(temp_input_file_name)
                print(f"Cleaned up temporary input file: {temp_input_file_name}")
            except OSError as e:
                print(f"Error removing temp_input_file {temp_input_file_name} in finally: {e}", file=sys.stderr)


@app.route('/download/<filename>')
def download_file(filename):
    """Serves files from the PROCESSED_VIDEOS_TEMP_DIR."""
    # This route is a generic download, but its usage is superseded by download_video/download_audio
    # It's kept for compatibility if any old links use it.
    return send_from_directory(app.config['PROCESSED_VIDEOS_TEMP_DIR'], filename, as_attachment=True) # Changed to send_from_directory


@app.route('/download-audio/<filename>')
def download_audio(filename):
    """Serves processed audio files from the PROCESSED_AUDIO_TEMP_DIR."""
    # Corrected usage for send_from_directory
    try:
        return send_from_directory(
            app.config['PROCESSED_AUDIO_TEMP_DIR'],
            filename,
            as_attachment=True,
            download_name=filename, # Ensure the download name is correct
            max_age=0 # Prevent caching by the browser/proxies for this specific download
        )
    except FileNotFoundError:
        print(f"File not found: {filename} in {app.config['PROCESSED_AUDIO_TEMP_DIR']}", file=sys.stderr)
        abort(404, "Processed audio file not found. It might have expired or been deleted.")
    except Exception as e:
        print(f"Error serving audio file {filename}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr) # Add traceback for server errors
        abort(500, f"An error occurred while serving the audio file: {e}")

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files from the UPLOAD_FOLDER."""
    print(f"Attempting to serve file: {filename} from {app.config['UPLOAD_FOLDER']}")
    try:
        # CORRECTED LINE: Use send_from_directory with the base directory and filename
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    except FileNotFoundError:
        print(f"File not found: {filename} in {app.config['UPLOAD_FOLDER']}", file=sys.stderr)
        abort(404, description=f"The file '{filename}' was not found.")
    except Exception as e:
        print(f"Error serving file {filename}: {e}", file=sys.stderr)
        abort(500, description=f"An error occurred while serving the file: {e}")

@app.route('/edit/pdf/view/<filename>')
def edit_pdf_viewer(filename):
    """Renders the PDF editing interface for a given PDF file."""
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.isfile(file_path):
        abort(404, f"File not found: {filename}")
    print(f"Rendering edit_pdf.html for file: {filename}")
    return render_template('edit_pdf.html', filename=filename)

@app.route('/edit/pdf', methods=['GET', 'POST'])
def edit_pdf():
    """Handles PDF file upload for editing."""
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            abort(400, "No file part in the request.")
        file = request.files['pdf_file']
        if file.filename == '':
            abort(400, "No selected file for upload.")
        if file and file.filename.lower().endswith('.pdf'):
            filename = secure_filename(file.filename)
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            try:
                file.save(path)
                print(f"PDF uploaded successfully: {filename} to {path}")
                # This is the crucial redirection after successful upload
                return redirect(url_for('edit_pdf_viewer', filename=filename))
            except Exception as e:
                print(f"Error saving uploaded PDF file: {e}", file=sys.stderr)
                abort(500, f"Error saving file: {e}")
        else:
            abort(400, "Invalid file type. Only PDF files are allowed.")
    # For GET requests, render the upload page
    return render_template('upload_pdf_editor.html')

@app.route('/save_edited_pdf', methods=['POST'])
def save_edited_pdf():
    """Saves the edited PDF with added elements (text, signatures, checkmarks, crosses)."""
    print("Received POST request to /save_edited_pdf")
    data = request.get_json()
    if not data:
        print("Error: No JSON data received.", file=sys.stderr)
        abort(400, "No JSON data received.")

    original_filename = data.get('filename')
    elements_data = data.get('elements', [])

    browser_canvas_dimensions = data.get('browserCanvasDimensions')
    pdf_native_dimensions = data.get('pdfNativeDimensions')

    print(f"Original Filename: {original_filename}")
    print(f"Browser Canvas Dimensions (from frontend, CSS px): {browser_canvas_dimensions}")
    print(f"PDF Native Dimensions (from frontend, PDF pts): {pdf_native_dimensions}")
    print(f"Elements Data received: {len(elements_data)} elements")

    # Frontend's ELEMENT_CONTENT_PADDING and ELEMENT_BORDER_WIDTH constants (must match frontend CSS)
    ELEMENT_CONTENT_PADDING = 5 # Matches 'padding: 5px;' in frontend
    ELEMENT_BORDER_WIDTH = 1    # Matches 'border: 1px dashed transparent;' in frontend
    TOTAL_ELEMENT_FRAME_SIZE = (ELEMENT_CONTENT_PADDING + ELEMENT_BORDER_WIDTH) * 2

    if not original_filename or not browser_canvas_dimensions or not pdf_native_dimensions:
        print("Error: Missing filename, browserCanvasDimensions, or pdfNativeDimensions in request.", file=sys.stderr)
        abort(400, "Missing filename, browserCanvasDimensions, or pdfNativeDimensions in request.")

    original_pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
    if not os.path.exists(original_pdf_path):
        print(f"Error: Original PDF '{original_filename}' not found at {original_pdf_path}", file=sys.stderr)
        abort(404, f"Original PDF '{original_filename}' not found.")

    doc = None # Initialize doc to None
    temp_image_files_for_cleanup = [] # List to store paths of temporary image files for cleanup

    try:
        # Open the original PDF with PyMuPDF
        doc = fitz.open(original_pdf_path)
        print(f"PyMuPDF document opened. Number of pages: {doc.page_count}")

        for page_number in range(doc.page_count):
            page = doc[page_number] # Get the current page object
            print(f"\n--- Processing Page {page_number + 1} (0-indexed: {page_number}) ---")

            # Get actual page dimensions from PyMuPDF for accurate drawing context
            current_page_width, current_page_height = page.mediabox_size.x, page.mediabox_size.y
            print(f"  PyMuPDF Page {page_number} dimensions: W={current_page_width:.2f}, H={current_page_height:.2f}")

            # Calculate scaling factors from frontend CSS pixels to backend PDF points
            scale_x_css_to_pdf = (pdf_native_dimensions['width'] / browser_canvas_dimensions['width']) if browser_canvas_dimensions['width'] > 0 else 1.0
            scale_y_css_to_pdf = (pdf_native_dimensions['height'] / browser_canvas_dimensions['height']) if browser_canvas_dimensions['height'] > 0 else 1.0

            print(f"  Scaling Factors (CSS px to PDF pt): X={scale_x_css_to_pdf:.4f}, Y={scale_y_css_to_pdf:.4f}")

            # Use a BytesIO object for ReportLab content for this page (ONLY FOR TEXT)
            packet = io.BytesIO()
            # Initialize ReportLab canvas with the actual page dimensions in points
            can = rl_canvas.Canvas(packet, pagesize=(current_page_width, current_page_height))

            # Flag to check if any ReportLab content was drawn on this page
            drawn_reportlab_content_on_current_page = False

            for element in elements_data:
                el_type = element.get('type')
                el_content = element.get('content')

                element_page_num_0_indexed = element.get('pageNum', 1) - 1

                if element_page_num_0_indexed == page_number:
                    # Retrieve raw CSS pixel coordinates and dimensions from frontend
                    x_css = float(element.get('x_css', 0.0))
                    y_css = float(element.get('y_css', 0.0))
                    width_css_total = float(element.get('width_css', 0.0))
                    height_css_total = float(element.get('height_css', 0.0))

                    print(f"  Applying element (ID: {element.get('id', 'N/A')}, Type: {el_type}) to page {page_number}:")
                    print(f"    Frontend CSS coords (x,y,w_total,h_total): ({x_css:.2f}, {y_css:.2f}, {width_css_total:.2f}, {height_css_total:.2f})")

                    # Convert frontend CSS pixel coordinates (outer div) to content-area CSS pixels
                    x_content_css = x_css + ELEMENT_CONTENT_PADDING + ELEMENT_BORDER_WIDTH
                    y_content_css = y_css + ELEMENT_CONTENT_PADDING + ELEMENT_BORDER_WIDTH

                    # Convert content-area CSS pixels to PDF points
                    x_pos_pdf = x_content_css * scale_x_css_to_pdf
                    y_pos_pdf = y_content_css * scale_y_css_to_pdf

                    # Adjust content dimensions (subtract frame size) before converting to PDF points
                    width_content_css = width_css_total - TOTAL_ELEMENT_FRAME_SIZE
                    height_content_css = height_css_total - TOTAL_ELEMENT_FRAME_SIZE

                    width_el_pdf = width_content_css * scale_x_css_to_pdf
                    height_el_pdf = height_content_css * scale_y_css_to_pdf

                    # Apply empirical offsets (in PDF points) for final alignment
                    x_pos_pdf += PDF_RENDER_ADJUSTMENT_X_BACKEND
                    y_pos_pdf += PDF_RENDER_ADJUSTMENT_Y_BACKEND

                    if width_el_pdf <= 0 or height_el_pdf <= 0:
                        print(f"    Warning: Element dimensions are zero or negative ({width_el_pdf:.2f}, {height_el_pdf:.2f}). Skipping drawing for this element.", file=sys.stderr)
                        continue

                    # PyMuPDF's Y-axis origin is top-left, while ReportLab's is bottom-left.
                    # For elements drawn directly with PyMuPDF (images, SVGs), `y_pos_pdf` is the top-left Y coordinate.
                    # For ReportLab elements (text), `y_pos_reportlab` is the bottom-left Y coordinate relative to ReportLab's canvas.

                    if el_type == 'textbox' or el_type == 'datebox':
                        drawn_reportlab_content_on_current_page = True
                        style = element.get('style', {})
                        font_size_px = float(style.get('fontSize', '16px').replace('px', ''))
                        color_hex = style.get('color', '#000000')
                        font_weight = style.get('fontWeight', 'normal')
                        font_style = style.get('fontStyle', 'normal')

                        font_size_pt = font_size_px * (72 / 96) # Convert browser pixels to PDF points

                        rl_font = 'Helvetica'
                        if 'Arial' in style.get('fontFamily', ''): rl_font = 'Helvetica'
                        elif 'Verdana' in style.get('fontFamily', ''): rl_font = 'Helvetica'
                        elif 'Times New Roman' in style.get('fontFamily', ''): rl_font = 'Times-Roman'
                        elif 'Courier New' in style.get('fontFamily', ''): rl_font = 'Courier'
                        elif 'Georgia' in style.get('fontFamily', ''): rl_font = 'Times-Roman'

                        if font_weight == 'bold' and font_style == 'italic':
                            rl_font = f'{rl_font}-BoldOblique'
                        elif font_weight == 'bold':
                            rl_font = f'{rl_font}-Bold'
                        elif font_style == 'italic':
                            rl_font = f'{rl_font}-Oblique'

                        can.setFont(rl_font, font_size_pt)

                        try:
                            can.setFillColor(HexColor(color_hex))
                        except Exception as color_e:
                            print(f"    Warning: Invalid color hex '{color_hex}', defaulting to black. Error: {color_e}", file=sys.stderr)
                            can.setFillColorRGB(0, 0, 0) # Fallback to black

                        # ReportLab's Y-axis is from the bottom, PyMuPDF's is from the top.
                        # `y_pos_pdf` is from top of page (PyMuPDF coordinate).
                        # We need to convert it to ReportLab's bottom-origin Y for text.
                        # ReportLab Y = Page Height - PyMuPDF Y - (Element Height in PDF points)
                        y_pos_reportlab = current_page_height - y_pos_pdf - height_el_pdf
                        can.drawString(x_pos_pdf, y_pos_reportlab, el_content)
                        print(f"    Drawing text: '{el_content}' at ({x_pos_pdf:.2f}, {y_pos_reportlab:.2f}) on ReportLab canvas.")

                    elif el_type == 'signature':
                        img_data_url = el_content

                        print(f"    Signature data URL length: {len(img_data_url) if img_data_url else 0}")
                        print(f"    Signature dimensions (PDF points): ({width_el_pdf:.2f}, {height_el_pdf:.2f})")

                        if img_data_url and ',' in img_data_url:
                            try:
                                header, base64_data = img_data_url.split(',', 1)
                                decoded_image_bytes = base64.b64decode(base64_data)

                                image_io = io.BytesIO(decoded_image_bytes)
                                img = Image.open(image_io)

                                if img.mode != 'RGBA':
                                    img = img.convert('RGBA')

                                temp_fd, temp_path = tempfile.mkstemp(suffix=".png")
                                os.close(temp_fd) # Close the file descriptor immediately
                                img.save(temp_path, format='PNG')
                                temp_image_files_for_cleanup.append(temp_path)

                                # Define the rectangle for PyMuPDF: (x0, y0, x1, y1)
                                # PyMuPDF's y-axis is from the top.
                                # x0, y0 = top-left corner
                                # x1, y1 = bottom-right corner
                                rect = fitz.Rect(x_pos_pdf, y_pos_pdf, x_pos_pdf + width_el_pdf, y_pos_pdf + height_el_pdf)
                                page.insert_image(rect, filename=temp_path) # Use filename directly with PyMuPDF
                                print(f"    Successfully inserted signature image using PyMuPDF at {rect}.")

                            except Exception as img_e:
                                print(f"Error processing image for signature (element ID: {element.get('id', 'N/A')}): {img_e}", file=sys.stderr)
                                traceback.print_exc(file=sys.stderr)
                                print("    Attempting to continue processing other elements. (Check image validity from frontend)", file=sys.stderr)
                        else:
                            print(f"    Warning: Signature content not valid data URL for element ID: {element.get('id', 'N/A')}", file=sys.stderr)

                    elif el_type == 'checkmark' or el_type == 'cross':
                        # CRITICAL CHECK: Ensure cairosvg is imported successfully
                        if not cairosvg:
                            print(f"    Skipping SVG insertion for '{el_type}' as cairosvg is not available. Please install system dependencies and cairosvg.", file=sys.stderr)
                            continue

                        svg_content = el_content
                        print(f"    Attempting to insert SVG of type '{el_type}' at ({x_pos_pdf:.2f}, {y_pos_pdf:.2f}) with size ({width_el_pdf:.2f}, {height_el_pdf:.2f}).")

                        # The rect defines the bounding box where the SVG will be placed.
                        # PyMuPDF's y-axis is from the top.
                        rect = fitz.Rect(x_pos_pdf, y_pos_pdf, x_pos_pdf + width_el_pdf, y_pos_pdf + height_el_pdf)

                        # Extract color from style (if available) and apply to SVG
                        style = element.get('style', {})
                        color_hex = style.get('color', '#000000') # Default black

                        # Simple SVG color injection if fill is "currentColor"
                        if "currentColor" in svg_content:
                            svg_content_with_color = svg_content.replace('fill="currentColor"', f'fill="{color_hex}"')
                        else:
                            svg_content_with_color = svg_content

                        try:
                            # NEW: Convert SVG content to PNG bytes using CairoSVG for robust embedding
                            # Target a high resolution PNG for better quality, fitz will scale it.
                            # Aim for 3x the eventual PDF point size for the PNG pixels to ensure good resolution.
                            output_png_width_px = int(width_el_pdf * 3)
                            output_png_height_px = int(height_el_pdf * 3)

                            # Ensure minimum dimensions for CairoSVG to avoid errors with very small values
                            output_png_width_px = max(1, output_png_width_px)
                            output_png_height_px = max(1, output_png_height_px)

                            png_bytes = cairosvg.svg2png(
                                bytestring=svg_content_with_color.encode('utf-8'),
                                output_width=output_png_width_px,
                                output_height=output_png_height_px
                            )

                            # Insert the PNG bytes directly using PyMuPDF's `stream` option
                            page.insert_image(rect, stream=png_bytes)
                            print(f"    Successfully converted SVG to PNG and inserted for '{el_type}' using PyMuPDF at {rect}.")

                        except Exception as svg_e:
                            print(f"    Error processing SVG (converting to PNG) for '{el_type}' (element ID: {element.get('id', 'N/A')}): {svg_e}", file=sys.stderr)
                            traceback.print_exc(file=sys.stderr)
                            print("    Attempting to continue processing other elements. (Check SVG validity or CairoSVG system setup)", file=sys.stderr)

                else:
                    print(f"  Element (ID: {element.get('id', 'N/A')}) skipped for page {page_number} (belongs to page {element_page_num_0_indexed}).")


            can.save() # Save ReportLab content for this page
            print(f"ReportLab canvas saved for page {page_number}.")

            overlay_pdf_bytes = packet.getvalue()
            # Only overlay if ReportLab actually drew something on this page
            if drawn_reportlab_content_on_current_page and overlay_pdf_bytes:
                try:
                    overlay_doc = fitz.open("pdf", overlay_pdf_bytes)
                    if overlay_doc.page_count > 0:
                        overlay_page = overlay_doc[0]
                        print(f"Overlay page extracted from ReportLab output for page {page_number}.")
                        # Overlay the ReportLab content (text) onto the PyMuPDF page.
                        # This should now work alongside directly inserted images/SVGs.
                        page.show_pdf_page(page.rect, overlay_doc, pno=0)
                        print(f"Page {page_number} overlayed using PyMuPDF's show_pdf_page for ReportLab (text) content.")
                    else:
                        print(f"Info: ReportLab canvas for page {page_number} produced an empty PDF (no pages). Skipping overlay for this page.", file=sys.stderr)
                except Exception as overlay_e:
                    print(f"Error opening/overlaying ReportLab PDF with PyMuPDF for page {page_number}: {overlay_e}", file=sys.stderr)
            else:
                print(f"Info: No text elements drawn with ReportLab on page {page_number}, or ReportLab output was empty. Skipping text overlay for this page.")


        output_filename = "edited_" + original_filename
        final_pdf_bytes_io = io.BytesIO()
        doc.save(final_pdf_bytes_io, garbage=4, deflate=True, clean=True)
        final_pdf_bytes_io.seek(0)

        print(f"Edited PDF saved to in-memory BytesIO.")

        return send_file(final_pdf_bytes_io, as_attachment=True, mimetype='application/pdf', download_name=output_filename)

    except Exception as e:
        print(f"CRITICAL ERROR during PDF save process: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        if "Badly formed" in str(e) or "Not a PDF" in str(e):
             abort(400, "The provided PDF file appears corrupted or is not a valid PDF.")
        abort(500, f"Failed to save edited PDF: {e}")
    finally:
        # Clean up temporary image files
        for fpath in temp_image_files_for_cleanup:
            try:
                os.remove(fpath)
                print(f"Cleaned up temporary image file: {fpath}")
            except OSError as e:
                print(f"Error cleaning up temp file {fpath}: {e}", file=sys.stderr)
        # Close the PyMuPDF document if it was opened successfully
        if doc:
            doc.close()
            print("PyMuPDF document closed.")

@app.route('/word-to-pdf', methods=['GET', 'POST'])
def word_to_pdf():
    """Handles Word to PDF conversion using unoconv."""
    if request.method == 'POST':
        if 'word_file' not in request.files:
            abort(400, "No file part in the request.")
        file = request.files['word_file']
        if file.filename == '':
            abort(400, "No selected file.")

        # Ensure that only .doc or .docx files are accepted
        if file and (file.filename.lower().endswith('.doc') or file.filename.lower().endswith('.docx')):
            original_filename = secure_filename(file.filename)
            input_filepath = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)

            # Save the uploaded Word file
            try:
                file.save(input_filepath)
                print(f"Uploaded Word file: {input_filepath}")
            except Exception as e:
                print(f"Error saving uploaded Word file {original_filename}: {e}", file=sys.stderr)
                abort(500, f"Failed to save uploaded file: {e}")

            # Define output path for the PDF in the CONVERTED_FOLDER
            base_filename = os.path.splitext(original_filename)[0]
            output_filename = base_filename + ".pdf"
            output_filepath = os.path.join(app.config['CONVERTED_FOLDER'], output_filename)

            try:
                # Command to convert using unoconv
                command = ['unoconv', '-f', 'pdf', '-o', output_filepath, input_filepath]

                print(f"Executing command: {' '.join(command)}")

                # Run the command, capturing output and checking for errors
                result = subprocess.run(command, capture_output=True, text=True, check=True)

                print("unoconv stdout:", result.stdout)
                if result.stderr:
                    print("unoconv stderr:", result.stderr)

                # Check if the output PDF was successfully created
                if os.path.exists(output_filepath):
                    print(f"Conversion successful. Sending: {output_filepath}")
                    # Clean up the original uploaded Word file
                    try:
                        os.remove(input_filepath)
                    except OSError as e:
                        print(f"Error removing uploaded Word file {input_filepath}: {e}", file=sys.stderr)

                    return send_file(output_filepath, as_attachment=True, mimetype='application/pdf', download_name=output_filename)
                else:
                    print(f"Error: unoconv did not create the output file at {output_filepath}", file=sys.stderr)
                    abort(500, "Conversion failed: Output PDF was not generated. Check server logs for unoconv output.")

            except subprocess.CalledProcessError as e:
                # This exception is caught if unoconv returns a non-zero exit code (an error)
                print(f"unoconv failed with error code {e.returncode}", file=sys.stderr)
                print(f"stdout: {e.stdout}", file=sys.stderr)
                print(f"stderr: {e.stderr}", file=sys.stderr)
                abort(500, f"Word to PDF conversion failed: Command execution error. Details: {e.stderr}. "
                           "Ensure LibreOffice is installed and unoconv can find it (check UNO_PATH).")
            except FileNotFoundError:
                # This exception is caught if the 'unoconv' command itself is not found
                print("Error: 'unoconv' command not found. Please ensure LibreOffice and unoconv are installed and in your system's PATH.", file=sys.stderr)
                abort(500, "Word to PDF conversion service not configured. Please install LibreOffice and unoconv.")
            except Exception as e:
                # Catch any other unexpected errors during the process
                print(f"An unexpected error occurred during Word to PDF conversion: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr) # Print full traceback for debugging
                abort(500, f"An internal server error occurred during conversion: {e}")

    return render_template('word_to_pdf.html') # For GET requests, show the upload form

@app.route('/convert/pdf-to-word', methods=['GET', 'POST'])
def pdf_to_word():
    """Handles PDF to Word conversion."""
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            return 'No file part'
        file = request.files['pdf_file']
        if file.filename == '':
            return 'No selected file'
        if file:
            filename = secure_filename(file.filename)
            pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], filename) # Use app.config
            file.save(pdf_path)

            # Ensure the 'converted' folder exists for output
            converted_dir_path = app.config['CONVERTED_FOLDER']

            # Generate Word file path in the CONVERTED_FOLDER
            word_filename = os.path.splitext(filename)[0] + '.docx'
            word_path = os.path.join(converted_dir_path, word_filename)

            # Convert PDF to Word
            cv = Converter(pdf_path)
            cv.convert(word_path, start=0, end=None)
            cv.close()

            # Clean up the uploaded PDF file
            try:
                os.remove(pdf_path)
            except OSError as e:
                print(f"Error removing uploaded PDF file {pdf_path}: {e}", file=sys.stderr)

            # Send converted file for download
            return send_file(word_path, as_attachment=True, download_name=word_filename)

    return render_template('pdf_to_word.html')


@app.route('/split-pdf', methods=['GET', 'POST'])
def split_pdf():
    """Handles splitting a PDF into selected pages."""
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            abort(400, "No PDF file provided for splitting.")
        pdf_file_storage = request.files['pdf_file']
        pages_input = request.form.get('pages', '')

        if not pdf_file_storage.filename or not pages_input:
            abort(400, "PDF file and pages to keep are required.")

        try:
            pages_to_keep_raw = [p.strip() for p in pages_input.split(',')]
            pages_to_keep = []
            for p_str in pages_to_keep_raw:
                try:
                    page_num = int(p_str)
                    if page_num <= 0:
                        raise ValueError("Page numbers must be positive.")
                    pages_to_keep.append(page_num - 1)
                except ValueError:
                    abort(400, f"Invalid page number '{p_str}'. Please enter comma-separated integers.")

        except Exception as e:
            abort(400, f"Invalid page numbers format: {e}")

        try:
            pdf_reader = PdfReader(pdf_file_storage)
            pdf_writer = PdfWriter()

            max_pages = len(pdf_reader.pages)
            for i in pages_to_keep:
                if 0 <= i < max_pages:
                    pdf_writer.add_page(pdf_reader.pages[i])
                else:
                    print(f"Warning: Page {i + 1} is out of bounds (total pages: {max_pages}). Skipping.", file=sys.stderr)

            if not pdf_writer.pages:
                abort(400, "No valid pages were selected for splitting. Ensure page numbers are correct.")

            output_filename = "split_" + secure_filename(pdf_file_storage.filename)
            output_path = os.path.join(app.config['TRIMMED_FOLDER'], output_filename) # Using trimmed folder for split PDFs

            with open(output_path, 'wb') as f:
                pdf_writer.write(f)

            print(f"PDF split successfully. Sending file: {output_filename}")
            return send_file(output_path, as_attachment=True, mimetype='application/pdf', download_name=output_filename)

        except Exception as e:
            print(f"Error during splitting PDF: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            abort(400, f"Error during splitting PDF: {e}. Please check the file and page numbers.")

    return render_template('split_pdf.html')

@app.route('/png-to-jpg', methods=['GET', 'POST'])
def png_to_jpg():
    """Handles PNG to JPG image conversion."""
    if request.method == 'POST':
        if 'image_file' not in request.files:
            abort(400, "No image file provided for conversion.")
        image_file = request.files['image_file']

        if not image_file.filename:
            abort(400, "No selected file.")

        if not image_file.filename.lower().endswith('.png'):
            abort(400, "Invalid file type. Only PNG images are supported for PNG to JPG conversion.")

        try:
            img = Image.open(image_file)
            base_filename = os.path.splitext(secure_filename(image_file.filename))[0]
            output_filename = base_filename + ".jpg"
            output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename) # Use converted folder

            img.convert('RGB').save(output_path, "JPEG")
            print(f"PNG converted to JPG successfully: {output_filename}")
            return send_file(output_path, as_attachment=True, mimetype='image/jpeg', download_name=output_filename)
        except Exception as e:
            print(f"Error during PNG to JPG conversion: {e}", file=sys.stderr)
            abort(400, f"Error during conversion: {e}")
    return render_template('png_to_jpg.html') # Explicitly render the page for GET requests and on error

@app.route('/jpg-to-png', methods=['GET', 'POST'])
def jpg_to_png():
    """Handles JPG to PNG image conversion."""
    if request.method == 'POST':
        if 'image_file' not in request.files:
            return 'No image file provided for conversion.'
        file = request.files['image_file']

        if not file.filename:
            return 'No selected file.'

        if not file.filename.lower().endswith(('.jpg', '.jpeg')):
            return 'Invalid file type. Only JPG/JPEG images are supported for JPG to PNG conversion.'

        try:
            img = Image.open(file)
            base_filename = os.path.splitext(secure_filename(file.filename))[0]
            output_filename = base_filename + ".png"
            output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename) # Use converted folder

            img.save(output_path, "PNG")
            print(f"JPG converted to PNG successfully: {output_filename}")
            return send_file(output_path, as_attachment=True, mimetype='image/png', download_name=output_filename)
        except Exception as e:
            print(f"Error during JPG to PNG conversion: {e}", file=sys.stderr)
            abort(400, f"Error during conversion: {e}")
    return render_template('jpg_to_png.html') # Explicitly render the page for GET requests and on error

@app.route('/music', methods=['GET', 'POST'])
def music_page():
    """Renders the music (audio trimming/fading) page and handles audio trimming POST requests."""
    if request.method == 'POST':
        audio_file_storage = request.files['audio_file']
        output_format = request.form.get('output_format', 'mp3')

        allowed_formats = {
            'mp3': 'audio/mpeg',
            'wav': 'audio/wav',
            'm4a': 'audio/mp4',
            'mp4': 'video/mp4' # For audio from video, MP4 container is often used
        }
        if output_format not in allowed_formats:
            return jsonify(error=f"Unsupported output format: {output_format}"), 400

        try:
            start_time_sec = float(request.form['start_time'])
            end_time_sec = float(request.form['end_time'])

            if AudioSegment.converter is None:
                return jsonify(error="FFmpeg is not configured. Cannot process audio."), 500

            audio = AudioSegment.from_file(audio_file_storage)

            start_time_ms = start_time_sec * 1000
            end_time_ms = end_time_sec * 1000

            audio_duration_ms = len(audio)
            if start_time_ms < 0 or end_time_ms < 0:
                return jsonify(error="Start and end times cannot be negative."), 400
            if start_time_ms >= audio_duration_ms:
                return jsonify(error=f"Start time ({start_time_sec}s) is beyond audio duration ({audio_duration_ms / 1000}s)."), 400
            if end_time_ms > audio_duration_ms:
                end_time_ms = audio_duration_ms
                print(f"Warning: End time adjusted to audio duration ({audio_duration_ms / 1000}s).")
            if start_time_ms >= end_time_ms:
                return jsonify(error="End time must be greater than start time."), 400

            trimmed_audio = audio[start_time_ms:end_time_ms]

            original_filename_base = os.path.splitext(secure_filename(audio_file_storage.filename))[0]
            # Use UUID for unique filename
            unique_filename = f"trimmed_{original_filename_base}_{uuid.uuid4()}.{output_format}"
            output_path = os.path.join(app.config['PROCESSED_AUDIO_TEMP_DIR'], unique_filename) # Store in temp audio dir

            trimmed_audio.export(output_path, format=output_format)
            print(f"Audio trimmed successfully. Stored temporarily at: {output_path}")

            # Return a JSON response with the download URL and filename
            return jsonify(success=True, download_url=url_for('download_audio', filename=unique_filename), filename=unique_filename)

        except Exception as e:
            print(f"Error during audio trimming: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify(error=f"Error during audio trimming: {e}"), 500

    return render_template('music.html')

@app.route('/fade_audio', methods=['POST'])
def fade_audio():
    """Handles fading in/out of audio segments."""
    if request.method == 'POST':
        audio_file_storage = request.files.get('audio_file')
        fade_type = request.form.get('fade_type')
        fade_duration_ms = int(request.form.get('fade_duration_ms', 3000))
        start_time_sec = float(request.form.get('start_time', 0))
        end_time_sec = float(request.form.get('end_time', 0))
        output_format = request.form.get('output_format', 'mp3')

        if not audio_file_storage or not fade_type:
            return jsonify(error="Missing audio file or fade type."), 400
        if fade_type not in ['in', 'out']:
            return jsonify(error="Invalid fade type. Must be 'in' or 'out'."), 400

        allowed_formats = {
            'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'm4a': 'audio/mp4', 'mp4': 'video/mp4'
        }
        if output_format not in allowed_formats:
            return jsonify(error=f"Unsupported output format: {output_format}"), 400

        if AudioSegment.converter is None:
            return jsonify(error="FFmpeg is not configured. Cannot process audio."), 500

        try:
            audio = AudioSegment.from_file(audio_file_storage)

            start_time_ms = start_time_sec * 1000
            end_time_ms = end_time_sec * 1000

            if end_time_ms == 0 and end_time_sec == 0:
                end_time_ms = len(audio)

            segment_length_ms = end_time_ms - start_time_ms
            actual_fade_duration_ms = min(fade_duration_ms, segment_length_ms)

            if start_time_ms > 0 or end_time_ms < len(audio):
                segment = audio[start_time_ms:end_time_ms]
                if fade_type == 'in':
                    faded_segment = segment.fade_in(actual_fade_duration_ms)
                else:
                    faded_segment = segment.fade_out(actual_fade_duration_ms)
                processed_audio = audio[:start_time_ms] + faded_segment + audio[end_time_ms:]
            else:
                if fade_type == 'in':
                    processed_audio = audio.fade_in(actual_fade_duration_ms)
                else:
                    processed_audio = audio.fade_out(actual_fade_duration_ms)

            original_filename_base = os.path.splitext(secure_filename(audio_file_storage.filename))[0]
            # Use UUID for unique filename
            unique_filename = f"faded_{fade_type}_{original_filename_base}_{uuid.uuid4()}.{output_format}"
            output_path = os.path.join(app.config['PROCESSED_AUDIO_TEMP_DIR'], unique_filename) # Store in temp audio dir

            processed_audio.export(output_path, format=output_format)
            print(f"Audio faded successfully. Stored temporarily at: {output_path}")

            # Return a JSON response with the download URL and filename
            return jsonify(success=True, download_url=url_for('download_audio', filename=unique_filename), filename=unique_filename)

        except Exception as e:
            print(f"Error during audio fading: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify(error=f"Error during audio fading: {e}"), 500

@app.route('/download-video/<filename>')
def download_video(filename):
    """
    Serves processed video files for download.
    The 'folder' query parameter specifies which folder to retrieve the file from.
    """
    folder = request.args.get('folder')
    # Use PROCESSED_VIDEOS_TEMP_DIR for both trimmed and merged videos, as it's the general temp output for video
    if folder == 'trimmed_videos' or folder == 'merged_videos':
        directory = app.config['PROCESSED_VIDEOS_TEMP_DIR'] # Correctly reference from app.config
    else:
        abort(400, "Invalid folder specified for download.")

    try:
        # CORRECTED USAGE
        return send_from_directory(directory, filename, as_attachment=True)
    except FileNotFoundError:
        print(f"File not found: {filename} in {directory}", file=sys.stderr)
        abort(404, "File not found.")
    except Exception as e:
        print(f"Error serving download file {filename} from {folder}: {e}", file=sys.stderr)
        abort(500, f"An error occurred while serving the file: {e}")


@app.route('/add_audio_track', methods=['POST'])
def add_audio_track():
    """Handles overlaying a new audio track onto existing media's audio."""
    if request.method == 'POST':
        main_media_file_storage = request.files.get('main_media_file')
        new_audio_file_storage = request.files.get('new_audio_file')
        output_format = request.form.get('output_format', 'mp3') # Default to mp3 as we are audio-only now

        if not main_media_file_storage or not new_audio_file_storage:
            abort(400, "Both a main media file and a new audio file are required.")

        if not new_audio_file_storage.filename.lower().endswith(('.mp3', '.wav', '.m4a')):
            abort(400, "New audio file must be MP3, WAV, or M4A.")

        allowed_formats = {
            'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'm4a': 'audio/mp4'
        }
        if output_format not in allowed_formats:
            abort(400, f"Unsupported output format: {output_format}")

        if AudioSegment.converter is None: # Corrected check
            return jsonify(error="FFmpeg is not configured. Cannot process audio."), 500

        main_media_temp_path = None
        new_audio_temp_path = None
        output_path = None
        try:
            main_media_temp_filename = secure_filename(main_media_file_storage.filename)
            main_media_temp_path = os.path.join(app.config['UPLOAD_FOLDER'], main_media_temp_filename)
            main_media_file_storage.save(main_media_temp_path)

            new_audio_temp_filename = secure_filename(new_audio_file_storage.filename)
            new_audio_temp_path = os.path.join(app.config['UPLOAD_FOLDER'], new_audio_temp_filename)
            new_audio_file_storage.save(new_audio_temp_path)
            print(f"Main media saved to {main_media_temp_path}, new audio to {new_audio_temp_path}.")

            main_audio = AudioSegment.from_file(main_media_temp_path)
            new_audio = AudioSegment.from_file(new_audio_temp_path)

            combined_audio = main_audio.overlay(new_audio)

            original_filename_base = os.path.splitext(secure_filename(main_media_file_storage.filename))[0]
            output_filename = f"mixed_{original_filename_base}.{output_format}"
            output_path = os.path.join(app.config['MERGED_FOLDER'], output_filename) # Use merged folder

            combined_audio.export(output_path, format=output_format)
            print("Audio tracks mixed successfully using pydub.")
            return send_file(output_path, as_attachment=True, mimetype=allowed_formats[output_format], download_name=output_filename)

        except Exception as e:
            print(f"Error during adding audio track: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            abort(400, f"Error during adding audio track: {e}")
        finally:
            if main_media_temp_path and os.path.exists(main_media_temp_path):
                try: os.remove(main_media_temp_path)
                except OSError as e: print(f"Error removing main_media_temp_path in finally: {e}", file=sys.stderr)
            if new_audio_temp_path and os.path.exists(new_audio_temp_path):
                try: os.remove(new_audio_temp_path)
                except OSError as e: print(f"Error removing new_audio_temp_path in finally: {e}", file=sys.stderr)


@app.route('/add-audio', methods=['POST'])
def add_audio():
    """Handles merging (concatenating) multiple audio files."""
    if request.method == 'POST':
        audio_files_storage = request.files.getlist('audio_files')

        if not audio_files_storage or all(f.filename == '' for f in audio_files_storage):
            abort(400, "No audio files selected for merging.")

        if AudioSegment.converter is None: # Corrected check
            abort(500, "FFmpeg is not configured. Cannot process audio.")

        combined_audio = AudioSegment.empty()
        try:
            for audio_file in audio_files_storage:
                if audio_file.filename:
                    # Save to a temporary location first, then load with pydub
                    temp_input_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(audio_file.filename))
                    audio_file.save(temp_input_path)
                    audio = AudioSegment.from_file(temp_input_path)
                    combined_audio += audio
                    os.remove(temp_input_path) # Clean up temp input file

            if combined_audio.duration_seconds == 0:
                abort(400, "No valid audio content was merged.")

            output_filename = "combined_audio.mp3" # Merging typically outputs to a common format like MP3
            output_path = os.path.join(app.config['MERGED_FOLDER'], output_filename) # Use merged folder

            # Initialize file_data before the export
            file_data = None
            try:
                combined_audio.export(output_path, format="mp3")
                print(f"Audio merged successfully. Sending file: {output_filename}")
                with open(output_path, 'rb') as f:
                    file_data = f.read()
            finally:
                if os.path.exists(output_path):
                    os.remove(output_path) # Clean up temp file on server

            if file_data is not None:
                return send_file(io.BytesIO(file_data), mimetype='audio/mpeg', download_name=output_filename)
            else:
                return jsonify(error="Audio processing failed to produce output data."), 500

        except Exception as e:
            print(f"Error during audio merging: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr) # Print full traceback for debugging
            abort(400, f"Error during audio merging: {e}")

    return render_template('music.html')


@app.route('/audio-mixer')
def audio_mixer_page():
    """Renders the multi-track audio mixer HTML page."""
    return render_template('audio_mixer.html')


@app.route('/archive-extractor', methods=['GET'])
def archive_extractor_page():
    """Renders the archive extractor HTML page."""
    return render_template('archive_extractor.html')

@app.route('/extract-archive', methods=['POST'])
def extract_archive():
    """Handles the archive extraction process."""
    if not patoolib:
        abort(500, "patoolib library not loaded. Archive extraction is not available.")

    if 'archive_file' not in request.files:
        abort(400, "No archive file provided.")

    archive_file_storage = request.files['archive_file']
    if archive_file_storage.filename == '':
        abort(400, "No selected file.")

    original_filename = secure_filename(archive_file_storage.filename)
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)

    # Create a unique directory for extraction to avoid conflicts and for easy cleanup
    # Use original filename as base for the extraction directory name
    extract_dir_name = os.path.splitext(original_filename)[0] + "_" + os.urandom(4).hex()
    extract_path = os.path.join(app.config['EXTRACTED_FOLDER'], extract_dir_name)

    # Ensure extraction directory exists
    os.makedirs(extract_path, exist_ok=True)

    try:
        archive_file_storage.save(input_path)
        print(f"Archive saved to {input_path} for extraction.")

        # Extract the archive
        print(f"Attempting to extract archive '{input_path}' to '{extract_path}'...")
        patoolib.extract_archive(input_path, outdir=extract_path)
        print(f"Archive extracted successfully to {extract_path}.")

        # Create a zip file of the extracted contents for download
        # The base_name for make_archive will be the full path without extension,
        # so the zip file will be created in EXTRACTED_FOLDER
        output_zip_base_name = os.path.join(app.config['EXTRACTED_FOLDER'], os.path.splitext(original_filename)[0])
        output_zip_path = shutil.make_archive(output_zip_base_name, 'zip', root_dir=extract_path) # Corrected root_dir
        print(f"Created zip archive: {output_zip_path}")

        # Send the created zip file
        return send_file(output_zip_path, as_attachment=True, mimetype='application/zip',
                         download_name=os.path.basename(output_zip_path))

    except patoolib.util.PatoolError as pe:
        print(f"PatoolError during archive extraction: {pe}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        abort(400, f"Extraction failed: {pe}. Ensure required external tools (e.g., 7z, unrar) are installed for this archive type.")
    except Exception as e:
        print(f"Error during archive extraction: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        abort(500, f"An internal server error occurred during extraction: {e}")
    finally:
        # Clean up uploaded file and extraction directory
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
                print(f"Cleaned up uploaded archive: {input_path}")
            except OSError as e:
                print(f"Error removing uploaded archive {input_path}: {e}", file=sys.stderr)
        if os.path.exists(extract_path):
            try:
                shutil.rmtree(extract_path)
                print(f"Cleaned up extraction directory: {extract_path}")
            except OSError as e:
                print(f"Error removing extraction directory {extract_path}: {e}", file=sys.stderr)
        # Also clean up the final zip file after sending, if it exists and was sent
        # This is handled by send_file which often deletes temp files after sending,
        # but explicit cleanup here is safer if not attachment or if there's a delay.
        # For send_file(as_attachment=True), Flask often handles temp file deletion.
        # However, it's not deleted, this block would catch it.
        if 'output_zip_path' in locals() and output_zip_path and os.path.exists(output_zip_path):
            try:
                os.remove(output_zip_path)
                print(f"Cleaned up output zip file: {output_zip_path}")
            except OSError as e:
                print(f"Error removing output zip file {output_zip_path}: {e}", file=sys.stderr)

@app.route('/files-to-archiver', methods=['GET'])
def files_to_archiver_page():
    """Renders the files to archiver HTML page."""
    # Check for a 'success' query parameter to display a success message
    success_message = request.args.get('success')
    return render_template('files_to_archiver.html', success=success_message)

@app.route('/create-archive', methods=['POST'])
def create_archive():
    """Handles the creation of an archive from uploaded files."""
    temp_upload_dir = None
    output_archive_path = None

    print("\n--- create_archive POST Request Received ---")
    print(f"Request Form Data: {request.form}")
    print(f"Request Files Data: {request.files}")
    print("--- DEBUGGING END ---\n")

    try:
        # Create a temporary directory to store uploaded files before archiving
        temp_dir_name = "archive_source_" + os.urandom(4).hex()
        temp_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], temp_dir_name)
        os.makedirs(temp_upload_dir, exist_ok=True)

        files = request.files.getlist('files[]') # Use getlist with the correct name attribute

        # Filter out any empty file storage objects that might come from empty file inputs
        valid_files = [f for f in files if f.filename]

        if not valid_files:
            # If no valid files, clean up temp_upload_dir and return error
            if os.path.exists(temp_upload_dir):
                shutil.rmtree(temp_upload_dir)
            # Return a JSON error for fetch API to handle
            return jsonify(error="Please select at least one file to archive."), 400

        archive_format = request.form.get('archive_format')
        if not archive_format or archive_format not in ['zip', 'rar', '7z']:
            # If invalid format, clean up temp_upload_dir and return error
            if os.path.exists(temp_upload_dir):
                shutil.rmtree(temp_upload_dir)
            # Return a JSON error for fetch API to handle
            return jsonify(error="Invalid or missing archive format selected."), 400

        # Check if patoolib is needed and available for rar/7z
        if archive_format in ['rar', '7z'] and not patoolib:
            if os.path.exists(temp_upload_dir):
                shutil.rmtree(temp_upload_dir)
            return jsonify(error=f"patoolib library not loaded. Cannot create {archive_format} archives. Please install patool and required external tools (rar, 7z)."), 500


        uploaded_file_paths = []
        for file_storage in valid_files:
            filename = secure_filename(file_storage.filename)
            file_path = os.path.join(temp_upload_dir, filename)
            file_storage.save(file_path)
            uploaded_file_paths.append(file_path)
            print(f"Uploaded file saved to: {file_path}")

        archive_base_name = os.path.join(app.config['CREATED_ARCHIVES_FOLDER'], f"archive_{os.urandom(4).hex()}")

        if archive_format == 'zip':
            output_archive_path = shutil.make_archive(archive_base_name, 'zip', root_dir=temp_upload_dir)
            print(f"Created ZIP archive: {output_archive_path}")
        elif archive_format in ['rar', '7z']:
            output_archive_path = f"{archive_base_name}.{archive_format}"
            print(f"Attempting to create {archive_format} archive: {output_archive_path} from {temp_upload_dir}...")
            try:
                # patoolib expects a list of files/directories to archive.
                # If you want to archive the *contents* of temp_upload_dir, pass its path.
                patoolib.create_archive(output_archive_path, [temp_upload_dir])
                print(f"Created {archive_format} archive: {output_archive_path}")
            except patoolib.util.PatoolError as pe:
                # Clean up temp_upload_dir and return error
                if os.path.exists(temp_upload_dir):
                    shutil.rmtree(temp_upload_dir)
                # Return a JSON error for fetch API to handle
                return jsonify(error=f"Archive creation failed for {archive_format}: {pe}. Ensure external tools are installed."), 400

        if not output_archive_path or not os.path.exists(output_archive_path):
            raise Exception(f"Archive creation failed for format: {archive_format}. Output file not found.")

        # Send the created archive file
        return send_file(output_archive_path, as_attachment=True, mimetype=f'application/{archive_format}',
                         download_name=os.path.basename(output_archive_path))

    except Exception as e:
        print(f"Error during archive creation: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        # Ensure temp_upload_dir is cleaned up on error before rendering error page
        if temp_upload_dir and os.path.exists(temp_upload_dir):
            try:
                shutil.rmtree(temp_upload_dir)
                print(f"Cleaned up temporary source directory on error: {temp_upload_dir}")
            except OSError as cleanup_e:
                print(f"Error during error cleanup of {temp_upload_dir}: {cleanup_e}", file=sys.stderr)
        # Return a JSON error for fetch API to handle
        return jsonify(error=f"An internal server error occurred during archiving: {e}"), 500
    finally:
        # The cleanup for output_archive_path is generally handled by send_file.
        # Cleanup for temp_upload_dir is handled in the try/except blocks.
        pass

@app.route('/merge-pdf', methods=['GET'])
def merge_pdf():
    """Renders the PDF merge HTML page."""
    return render_template('merge_pdf.html')

@app.route('/merge-pdfs', methods=['POST'])
def merge_pdfs():
    """Handles the merging of multiple PDF files."""
    temp_upload_dir = None
    merged_pdf_path = None

    try:
        # Create a temporary directory to store uploaded PDFs
        temp_dir_name = "pdf_merge_source_" + os.urandom(4).hex()
        temp_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], temp_dir_name)
        os.makedirs(temp_upload_dir, exist_ok=True)

        if 'pdf_files' not in request.files:
            return jsonify(error="No PDF files provided for merging."), 400

        pdf_files_storage = request.files.getlist('pdf_files')

        valid_pdf_paths = []
        for file_storage in pdf_files_storage:
            if file_storage.filename and file_storage.filename.lower().endswith('.pdf'):
                filename = secure_filename(file_storage.filename)
                file_path = os.path.join(temp_upload_dir, filename)
                file_storage.save(file_path)
                valid_pdf_paths.append(file_path)
                print(f"Uploaded PDF saved to: {file_path}")
            else:
                print(f"Skipping non-PDF or empty file: {file_storage.filename}", file=sys.stderr)

        if len(valid_pdf_paths) < 2:
            return jsonify(error="Please upload at least two valid PDF files to merge."), 400

        merger = PdfMerger()
        for pdf_path in valid_pdf_paths:
            merger.append(pdf_path)

        merged_pdf_filename = f"merged_pdf_{os.urandom(4).hex()}.pdf"
        merged_pdf_path = os.path.join(app.config['MERGED_FOLDER'], merged_pdf_filename)

        merger.write(merged_pdf_path)
        merger.close()
        print(f"PDFs merged successfully to: {merged_pdf_path}")

        return send_file(merged_pdf_path, as_attachment=True, mimetype='application/pdf', download_name=merged_pdf_filename)

    except Exception as e:
        print(f"Error during PDF merging: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"An internal server error occurred during PDF merging: {e}"), 500
    finally:
        # Clean up temporary uploaded PDF files and the source directory
        if temp_upload_dir and os.path.exists(temp_upload_dir):
            try:
                shutil.rmtree(temp_upload_dir)
                print(f"Cleaned up temporary PDF merge source directory: {temp_upload_dir}")
            except OSError as e:
                print(f"Error removing temporary PDF merge source directory {temp_upload_dir}: {e}", file=sys.stderr)
        # The merged PDF file is handled by send_file for cleanup.
        pass

@app.route('/image-to-pdf', methods=['GET'])
def image_to_pdf():
    """Renders the image to PDF conversion HTML page."""
    return render_template('image_to_pdf.html')

@app.route('/convert-image-to-pdf', methods=['POST'])
def convert_image_to_pdf():
    """Handles the conversion of an image file to PDF."""
    temp_image_path = None
    temp_pil_image_path = None # Initialize temp_pil_image_path

    try:
        if 'image_file' not in request.files:
            return jsonify(error="No image file provided for conversion."), 400

        image_file_storage = request.files['image_file']

        if not image_file_storage.filename or not image_file_storage.filename.lower().endswith(('.png', '.jpg', '.jpeg')):
            return jsonify(error="Invalid file type. Please upload a PNG or JPG image."), 400

        # Save the uploaded image to a temporary file
        original_filename = secure_filename(image_file_storage.filename)
        temp_image_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        image_file_storage.save(temp_image_path)
        print(f"Uploaded image saved to: {temp_image_path}")

        # Open the image with Pillow
        img = Image.open(temp_image_path)

        # Create a PDF in memory
        output_pdf_buffer = io.BytesIO()

        # Determine page size based on image dimensions, maintaining aspect ratio
        # ReportLab uses points (1/72 inch). A4 is 595x842 points.
        # We'll scale the image to fit within an A4 page, or use image dimensions if smaller.
        # Max A4 dimensions
        A4_WIDTH = 595
        A4_HEIGHT = 842

        img_width, img_height = img.size

        # Calculate scaling factor to fit image within an A4 page while maintaining aspect ratio
        scale_factor = min(A4_WIDTH / img_width, A4_HEIGHT / img_height)

        # Calculate new dimensions for the image on the PDF page
        new_img_width = img_width * scale_factor
        new_img_height = img_height * scale_factor

        # Create a canvas with A4 size
        c = rl_canvas.Canvas(output_pdf_buffer, pagesize=(A4_WIDTH, A4_HEIGHT))

        # Calculate position to center the image on the A4 page
        x_offset = (A4_WIDTH - new_img_width) / 2
        y_offset = (A4_HEIGHT - new_img_height) / 2

        # Draw the image onto the PDF canvas
        # Ensure image is in RGB mode for ReportLab if it's RGBA (transparency)
        if img.mode == 'RGBA':
            img = img.convert('RGB')

        # Save image to a temporary file for ReportLab to read
        temp_pil_image_fd, temp_pil_image_path = tempfile.mkstemp(suffix=".png") # ReportLab prefers file paths
        os.close(temp_pil_image_fd) # Close file descriptor immediately
        img.save(temp_pil_image_path, format='PNG')

        c.drawImage(temp_pil_image_path, x_offset, y_offset,
                    width=new_img_width, height=new_img_height,
                    preserveAspectRatio=True)
        c.save()

        output_pdf_buffer.seek(0) # Rewind the buffer to the beginning

        # Generate output filename
        base_filename = os.path.splitext(original_filename)[0]
        output_pdf_filename = f"{base_filename}.pdf"

        return send_file(output_pdf_buffer, as_attachment=True, mimetype='application/pdf', download_name=output_pdf_filename)

    except Exception as e:
        print(f"Error during image to PDF conversion: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"An internal server error occurred during image to PDF conversion: {e}"), 500
    finally:
        # Ensure temporary files are cleaned up even if an error occurs
        if temp_image_path and os.path.exists(temp_image_path):
            try:
                os.remove(temp_image_path)
            except OSError as e:
                print(f"Error removing temp_image_path in finally: {e}", file=sys.stderr)
        if temp_pil_image_path and os.path.exists(temp_pil_image_path):
            try:
                os.remove(temp_pil_image_path)
            except OSError as e:
                print(f"Error removing temp_pil_image_path in finally: {e}", file=sys.stderr)


@app.route('/lock-pdf', methods=['GET'])
def lock_pdf_page():
    """Renders the PDF lock HTML page."""
    return render_template('lock_pdf.html')

@app.route('/process-lock-pdf', methods=['POST'])
def process_lock_pdf():
    """Handles locking a PDF file with a password."""
    if 'pdf_file' not in request.files:
        return jsonify(error="No PDF file provided."), 400

    pdf_file_storage = request.files['pdf_file']
    password = request.form.get('password', '')

    if not pdf_file_storage.filename or not pdf_file_storage.filename.lower().endswith('.pdf'):
        return jsonify(error="Invalid file type. Please upload a PDF file."), 400

    if not password:
        return jsonify(error="Password cannot be empty."), 400

    temp_pdf_path = None
    try:
        # Save the uploaded PDF to a temporary file
        original_filename = secure_filename(pdf_file_storage.filename)
        temp_pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        pdf_file_storage.save(temp_pdf_path)
        print(f"Uploaded PDF saved to: {temp_pdf_path}")

        reader = PdfReader(temp_pdf_path)
        writer = PdfWriter()

        # Add all pages to the writer
        for page in reader.pages:
            writer.add_page(page)

        # Encrypt the PDF
        writer.encrypt(password)

        output_pdf_buffer = io.BytesIO()
        writer.write(output_pdf_buffer)
        output_pdf_buffer.seek(0) # Rewind the buffer to the beginning after writing

        locked_filename = f"locked_{os.path.splitext(original_filename)[0]}_{uuid.uuid4()}.pdf" # Unique output name

        print(f"PDF locked successfully. Sending file: {locked_filename}")
        return send_file(output_pdf_buffer, as_attachment=True, mimetype='application/pdf', download_name=locked_filename)

    except Exception as e:
        print(f"Error during PDF locking: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"An internal server error occurred during PDF locking: {e}"), 500
    finally:
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
                print(f"Cleaned up temporary input PDF: {temp_pdf_path}")
            except OSError as e:
                print(f"Error removing temp_pdf_path in finally: {e}", file=sys.stderr)


@app.route('/unlock-pdf', methods=['GET'])
def unlock_pdf_page():
    """Renders the PDF unlock HTML page."""
    return render_template('unlock_pdf.html')

@app.route('/process-unlock-pdf', methods=['POST'])
def process_unlock_pdf():
    """Handles unlocking a password-protected PDF file."""
    if 'pdf_file' not in request.files:
        return jsonify(error="No PDF file provided."), 400

    pdf_file_storage = request.files['pdf_file']
    password = request.form.get('password', '')

    if not pdf_file_storage.filename or not pdf_file_storage.filename.lower().endswith('.pdf'):
        return jsonify(error="Invalid file type. Please upload a PDF file."), 400

    temp_pdf_path = None
    try:
        # Save the uploaded PDF to a temporary file
        original_filename = secure_filename(pdf_file_storage.filename)
        temp_pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        pdf_file_storage.save(temp_pdf_path)
        print(f"Uploaded PDF saved to: {temp_pdf_path}")

        reader = PdfReader(temp_pdf_path)

        if reader.is_encrypted:
            if not password:
                return jsonify(error="PDF is encrypted. Please provide a password."), 400

            try:
                if not reader.decrypt(password):
                    return jsonify(error="Incorrect password."), 400
            except Exception as e:
                print(f"Decryption error: {e}", file=sys.stderr)
                return jsonify(error="Decryption failed. Please check the password and try again."), 400
        else:
            if password:
                print("Warning: PDF is not encrypted, but a password was provided. Proceeding without decryption.", file=sys.stderr)
            else:
                print("Info: PDF is not encrypted. Proceeding without password.", file=sys.stderr)

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        output_pdf_buffer = io.BytesIO()
        writer.write(output_pdf_buffer)
        output_pdf_buffer.seek(0) # Rewind the buffer to the beginning after writing

        unlocked_filename = f"unlocked_{os.path.splitext(original_filename)[0]}_{uuid.uuid4()}.pdf" # Unique output name

        print(f"PDF unlocked successfully. Sending file: {unlocked_filename}")
        return send_file(output_pdf_buffer, as_attachment=True, mimetype='application/pdf', download_name=unlocked_filename)

    except Exception as e:
        print(f"Error during PDF unlocking: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error=f"An internal server error occurred during PDF unlocking: {e}"), 500
    finally:
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
                print(f"Cleaned up temporary input PDF: {temp_pdf_path}")
            except OSError as e:
                print(f"Error removing temp_pdf_path in finally: {e}", file=sys.stderr)


@app.route('/success')
def success():
    """Simple success page."""
    return "File processed successfully!"

if __name__ == '__main__':
    # These os.makedirs calls are now mostly redundant if the fix above is in place
    # but don't hurt for local development
    os.makedirs(PROCESSED_VIDEOS_TEMP_DIR, exist_ok=True)
    os.makedirs(PROCESSED_AUDIO_TEMP_DIR, exist_ok=True)

    for folder in [UPLOAD_FOLDER, CONVERTED_FOLDER, MERGED_FOLDER, TRIMMED_FOLDER, EXTRACTED_FOLDER,
                   CREATED_ARCHIVES_FOLDER, SECURE_PDF_FOLDER]:
        os.makedirs(folder, exist_ok=True)

    app.run(debug=True)