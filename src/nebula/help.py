from PyQt6.QtWidgets import (QApplication, QMainWindow, QTextBrowser,
                             QVBoxLayout, QWidget)

from . import \
    update_utils  # Assuming update_utils is a module in the same package


class HelpWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nebula: Help and User Guide")
        self.resize(1200, 800)  # Adjusted for better readability
        self.initUI()
        self.center()

    def center(self):
        screen = QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def initUI(self):
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        text_browser = QTextBrowser(main_widget)
        text_browser.setStyleSheet(
            "background-color: #1e1e1e; color: white; font-family: 'Courier';"
        )  # VSCode theme colors and font
        text_browser.setReadOnly(True)
        text_browser.setHtml(self.get_help_content())
        main_layout.addWidget(text_browser)
        self.setCentralWidget(main_widget)

    def get_help_content(self):

        ai_notes_image = update_utils.return_path("Images_readme/ai_notes.png")
        enable_ai_suggestions_image = update_utils.return_path(
            "Images_readme/enable_ai_suggestions.png"
        )
        suggestions_available_image = update_utils.return_path(
            "Images_readme/suggestions_available.png"
        )

        previous_results_context_menu_image = update_utils.return_path(
            "Images_readme/previous_results_context_menu.png"
        )
        take_screenshot_image = update_utils.return_path(
            "Images_readme/take-screenshot.png"
        )
        search_area_image = update_utils.return_path("Images_readme/Search_area.png")
        update_utils.return_path("Images_readme/highlight_items.png")
        busy_image = update_utils.return_path("Images_readme/busy.png")
        take_notes_image = update_utils.return_path("Images_readme/take_notes.png")
        terminal_image = update_utils.return_path("Images_readme/terminal.png")
        eco_mode_image = update_utils.return_path("Images_readme/eco_mode.png")
        add_image = update_utils.return_path("Images_readme/add_image.png")
        blur_image = update_utils.return_path("Images_readme/blur.png")
        crop_image = update_utils.return_path("Images_readme/crop.png")
        draw_arrow_image = update_utils.return_path("Images_readme/draw_arrow.png")
        update_utils.return_path("Images_readme/draw_text.png")
        save_image = update_utils.return_path("Images_readme/save.png")
        select_color_image = update_utils.return_path("Images_readme/select_color.png")
        thickness_image = update_utils.return_path("Images_readme/thickness.png")
        heading_image = update_utils.return_path("Images_readme/heading.png")
        highlight_image = update_utils.return_path("Images_readme/highlight.png")
        replace_image = update_utils.return_path("Images_readme/replace.png")
        update_utils.return_path("Images_readme/search.png")
        undo_image = update_utils.return_path("Images_readme/undo.png")
        redo_image = update_utils.return_path("Images_readme/redo.png")
        add_image = update_utils.return_path("Images_readme/add_image.png")
        blur_image = update_utils.return_path("Images_readme/blur.png")
        crop_image = update_utils.return_path("Images_readme/crop.png")
        draw_arrow_image = update_utils.return_path("Images_readme/draw_arrow.png")
        update_utils.return_path("Images_readme/draw_text.png")
        save_image = update_utils.return_path("Images_readme/save.png")
        select_color_image = update_utils.return_path("Images_readme/select_color.png")
        thickness_image = update_utils.return_path("Images_readme/thickness.png")
        heading_image = update_utils.return_path("Images_readme/heading.png")
        highlight_image = update_utils.return_path("Images_readme/highlight.png")
        replace_image = update_utils.return_path("Images_readme/replace.png")
        update_utils.return_path("Images_readme/search.png")
        undo_image = update_utils.return_path("Images_readme/undo.png")
        redo_image = update_utils.return_path("Images_readme/redo.png")
        update_utils.return_path("Images_readme/configuration.png")
        clear_screen = update_utils.return_path("Images_readme/clear_screen.png")
        engagement_details = update_utils.return_path(
            "Images_readme/engagement_details.png"
        )
        context_menu_image = update_utils.return_path("Images_readme/context_menu.png")
        command_input_area_image = update_utils.return_path(
            "Images_readme/command_input_area.png"
        )
        code_analysis = update_utils.return_path("Images_readme/code_analysis.png")

        eclipse = update_utils.return_path("Images_readme/eclipse.png")
        help_content_html = f"""
<style>
    body {{ background-color: #1e1e1e; color: white; font-family: 'Courier'; }}
    h1, h2, h3, h4, h5, h6 {{ color: white; }}
    a {{ color: white; }}
    .note {{ background-color: #252526; margin: 10px 0; padding: 10px; border-left: 5px solid #4ec9b0; }}
    img {{ border: 2px solid #3f3f46; margin-top: 5px; }}
    ul, ol {{ padding-left: 20px; }}
    li {{ margin-bottom: 10px; }}
</style>

<h1>Nebula: AI-Driven PenTestOps Platform</h1>
<p>Nebula is a cutting-edge platform designed for ethical hackers, offering an AI-driven approach to identify and exploit security vulnerabilities efficiently. This toolkit combines advanced technology with user-friendly features to enhance the cybersecurity workflow.</p>
<h2>Getting Started</h2>
<h3>Home Screen</h3>
<ul>
    <li><strong>Sending Files for AI-Based Analysis:</strong> Send uploaded files or previous commands for suggestions or recommendations by using the context menu.<br><br><img src="{previous_results_context_menu_image}" alt="Sending Files for AI-Based Analysis"></li>
   
    
    <li><strong>Context Menu Interaction:</strong> Highlight any text to bring up the context menu for quick actions and features.<br><br><img src="{context_menu_image}" alt="Context Menu"></li>
    <li><strong>AI-Assisted Note-Taking:</strong> Automates documentation of crucial security findings, organizing them by CWE identifiers and aligning with NIST controls when applicable. <br><br><img src="{ai_notes_image}" alt="AI-Assisted Note-Taking"></li>
    <li><strong>AI-Based Suggestions:</strong> Activate this to receive AI-based suggestions for exploiting vulnerabilities by toggling the suggestions icon.<br><br><img src="{enable_ai_suggestions_image}" alt="AI-Based Suggestions"></li>
    <li><strong>Viewing Suggestions:</strong> When suggestions are available, the suggestions icon lights up. Click it to view the suggestions.<br><br><img src="{suggestions_available_image}" alt="Viewing Suggestions"></li>

    <li><strong>Integrated Screenshot and Editing:</strong> Simplifies capturing and annotating images within the toolkit. <br><br><img src="{take_screenshot_image}" alt="Integrated Screenshot and Editing"></li>
   
    <li><strong>Model Status:</strong> Indicates the operational status of the models. The busy icon will turn red while any of the models are busy.<br><br><img src="{busy_image}" alt="Model Status"></li>
    
    <li><strong>Taking Screenshots:</strong> Click the screenshot icon to capture the current screen.<br><br><img src="{take_screenshot_image}" alt="Taking Screenshots"></li>
    <li><strong>Command Search:</strong> Use the command search area to look for specific commands based on service names or protocols.<br><br><img src="{search_area_image}" alt="Command Search"></li>
    <li><strong>Manual Note-Taking:</strong> Click the note-taking icon to jot down your own notes. Notes are auto-saved.<br><br><img src="{take_notes_image}" alt="Manual Note-Taking"></li>
  <li><strong>Opening a New Terminal:</strong> Click the terminal icon to start a new terminal session.<br><img src="{terminal_image}" alt="Opening a New Terminal"></li>
    <li><strong>Eco Mode:</strong> Reduce data usage and credit consumption by activating Eco Mode when analyzing XML files. This feature is currently limited to Nessus, Nikto, NMAP and ZAP XML files.<br><br><img src="{eco_mode_image}" alt="Eco Mode"></li>
    <li><strong>Clear Screen:</strong> Click the clear screen icon to remove all content from the central display area.<br><br><img src="{clear_screen}" alt="Clear Screen"></li>
    <li><strong>View Engagement Details:</strong> Access detailed information about your current engagement by clicking the engagement details icon.<br><br><img src="{engagement_details}" alt="Engagement Details"></li>
    <li><strong>Command Input Area:</strong> Enter commands directly into the command input area at the bottom of the terminal. Start with an exclamation mark for AI assistant interactions.<br><br><img src="{command_input_area_image}" alt="Command Input Area"></li>
    <li><strong>Static Code Analysis:</strong> Click on the Code Analysis icon to open the code analysis window. Proceed to toggle the code analysis icon to activate it, then paste the code you wish to analyze<br><br><img src="{code_analysis}" alt="Code Analysis"></li>
    </ul>



<h3>Image and Note Editing Features</h3>
<ul>
    <li><strong>Adding Images:</strong> Select and image for editing by clicking on the 'Add Image' icon and selecting a file to upload.<br><img src="{add_image}" alt="Adding Images"></li>
    <li><strong>Blurring Parts of an Image:</strong> Enhance privacy by blurring out sensitive information in images. Select 'Blur', choose the area, and adjust the intensity.<br><img src="{blur_image}" alt="Blurring Parts of an Image"></li>
    <li><strong>Cropping Images:</strong> Trim unnecessary parts from your images by selecting 'Crop', dragging the box to the desired area, and applying the changes.<br><img src="{crop_image}" alt="Cropping Images"></li>
    <li><strong>Drawing Arrows:</strong> Highlight important sections or direct attention by selecting 'Draw Arrow', then choosing the start and end points on the image.<br><img src="{draw_arrow_image}" alt="Drawing Arrows"></li>
    <li><strong>Saving Changes:</strong> Ensure your edits are not lost by clicking 'Save' frequently during the editing process.<br><img src="{save_image}" alt="Saving Changes"></li>
    <li><strong>Selecting Colors:</strong> Customize the color of your annotations, such as text and arrows, by using 'Select Color' to pick from a palette.<br><img src="{select_color_image}" alt="Selecting Colors"></li>
    <li><strong>Adjusting Thickness:</strong> Change the thickness of your drawn elements (like arrows and lines) to make them more visible or subtle. Use 'Thickness' to choose the appropriate level.<br><img src="{thickness_image}" alt="Adjusting Thickness"></li>
    <li><strong>Adding Headings:</strong> Organize your notes effectively by using the 'Heading' option to add section titles. Ensure that the text you would like to make a heading is highlighted<br><img src="{heading_image}" alt="Adding Headings"></li>
    <li><strong>Highlighting:</strong> Emphasize important information by using the 'Highlight' tool to mark text and areas.Ensure that the text you would like to make a heading is highlighted<br><img src="{highlight_image}" alt="Highlighting"></li>
    <li><strong>Searching and Replacing Content:</strong>  Quickly find and replace specific pieces of text within your notes by using the 'Search and replace' function.<br><img src="{replace_image}" alt="Replacing Content"></li>
    <li><strong>Undoing Actions:</strong> Revert your last action if you make a mistake by clicking 'Undo'.<br><img src="{undo_image}" alt="Undoing Actions"></li>
    <li><strong>Redoing Actions:</strong> Reapply an action you've undone by clicking 'Redo'.<br><img src="{redo_image}" alt="Redoing Actions"></li>
    <li><strong>Eclipse:</strong>Provides a dedicated space for identifying sensitive information within text<br><img src="{eclipse}" alt="Eclipse"></li>
  </ul>

    </ul>


<h2>Feedback</h2>
<p>To provide feedback or report bugs, use the following formats:</p>
<pre>
    ? Thank you for such an amazing app!
    ?? I cannot seem to ask Nebula any questions, please help.
</pre>
<h2>More Help</h2>
<p>You can ask the Nebula AI assistants any questions answered in this manual using the following format</p>
<pre>
    ?! how do i turn on autonomous mode?
</pre>
<p><em>Nebula: Empowering ethical hackers with AI-driven precision and efficiency.</em></p>
"""
        return help_content_html


# Be sure to replace "{login_image}", "{logout_image}", etc. with the actual paths to the images as needed.
