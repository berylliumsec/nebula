#Nebula

Welcome to Nebula

![nebula](/images/nebula.webp)
## Acknowledgement

**First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.**

## News

Introducing the Deep Application Profiler (DAP). DAP uses neural networks to analyze an executable's internal structure and intent, rather than relying on traditional virus signatures. This approach enables it to detect new, zero-day malware that conventional methods often miss. DAP also provides detailed breakdowns for rapid analyst review and is available as both a web service and an API. [Learn More Here](https://www.berylliumsec.com/dap-overview)


Introducing Nebula Pro, Nebula Pro improves on Nebula 2.0 by adding additional features such as autonomous mode, code analysis and more. [Learn More Here](https://www.berylliumsec.com/nebula-pro-overview)

## Nebula: AI-Driven Penetration Testing Platform

Nebula is a cutting-edge toolkit designed for penetration testers. It integrates AI Models right into the command line interface and supports any tool that can be invoked from the CLI


## Installation

## System Requirements

- At least 8GB of GPU memory, we tested with 12GB
- Python >= 3.11

```bash
python -m pip install nebula-ai --upgrade
```

## Running Nebula

**important**

The first time you run Nebula, it will ask you to select a cache dir. The cache dir is where it will download the Model that you choose. For the models, you will need to create a free [Hugging Face Account](https://huggingface.co/), agree to the terms, generate an access token and export it to your CLI like so:


```bash
export HF_TOKEN=Your Token
```

This step only needs to be completed once. Monitor the command line interface where you invoked `nebula` from to monitor the download progress.

## Key Features

### Interacting with the models. 

To interact with the models, begin your input with a `!` for example: `! write a python script to scan the ports of a remote system`

### AI-Assisted Note-Taking
Automatically records pivotal security findings, categorizes them with CWE-IDs, and aligns them with NIST controls when applicable.

### Real-Time AI-Driven Insights
Provides immediate suggestions for discovering and exploiting vulnerabilities, based on outputs from integrated terminal tools.

### Enhanced Tool Integration
Seamlessly import data from external tools to capitalize on AI-powered note-taking and receive instant advice.

### Integrated Screenshot and Editing
Facilitates capturing and editing images within the toolkit for streamlined documentation.

### Manual Note-Taking
Offers a dedicated space for user-driven documentation, complementing the AI's automated notes. (Notes are auto-saved)

### Automatic Command Logging
Keeps a record of all commands executed within the terminal for easy reference and analysis.

## Getting Started

### Intuitive User Interface
Designed for ease of use, with tooltips for quick guidance and icons that intuitively represent their functions.

## User Guide

For a comprehensive video guide visit [here](https://www.berylliumsec.com/nebula-pro-feature-guide) and [here](https://www.youtube.com/playlist?list=PLySxaLbLL0gpAaDQYq6g6sb1q6KwqOAr4). Please note that some features are only applicable to Nebula Pro
### Home Screen
- **AI-Based Note-Taking:** Toggle the icon below. You will be prompted to select your a file to store your notes in  
  ![AI Notes](src/nebula/Images_readme/ai_notes.png)

- **AI Suggestions:** Activate by toggling the icon below.  
  ![Enable AI Suggestions](src/nebula/Images_readme/enable_ai_suggestions.png)

- **Viewing Suggestions:** When available, the icon lights up. Click to view.  
  ![Suggestions Available](src/nebula/Images_readme/suggestions_available.png)

- **Sending for Analysis:** Send uploaded files or previous commands for suggestions or recommendations.  
  ![Previous Results Context Menu](src/nebula/Images_readme/previous_results_context_menu.png)

- **Taking Screenshots:** Click the icon below.  
  ![Screenshot](src/nebula/Images_readme/take-screenshot.png)

- **Command Search:** Search and select commands, then hit enter to populate the command input area.  
  ![Search Area](src/nebula/Images_readme/Search_area.png)

- **Manual Note-Taking:** Click the icon below.  
  ![Take Notes](src/nebula/Images_readme/take_notes.png)

- **Opening a New Terminal:** Click the icon below.  
  ![Terminal](src/nebula/Images_readme/terminal.png)

### Image and Note Editing Features
- **Adding Images:** Click 'Add Image' and choose a file.  
  ![Add Image](src/nebula/Images_readme/add_image.png "Adding an Image")

- **Blurring Parts of an Image:** Select 'Blur', choose area, and adjust intensity.  
  ![Blur](src/nebula/Images_readme/blur.png "Blurring an Image")

- **Cropping Images:** Select 'Crop', drag the box, and apply.  
  ![Crop](src/nebula/Images_readme/crop.png "Cropping an Image")

- **Drawing Arrows:** Select 'Draw Arrow', choose start and end points.  
  ![Draw Arrow](src/nebula/Images_readme/draw_arrow.png "Drawing an Arrow")

- **Drawing Text:** Select 'The text icons', choose start and end points.  
  ![Draw Arrow](src/nebula/Images_readme/draw_arrow.png "Drawing an Arrow")
  Select 'Draw Text', click an area in the image to start typing. You can use backspace to undo text, note that once you click out of typed text, to undo you would need to click the undo button or CTRL + Z
- **Saving Changes:** Click 'Save' frequently to avoid data loss.  
  ![Save](src/nebula/Images_readme/save.png "Saving Changes")

- **Selecting Colors:** Use 'Select Color' to choose from the palette.  
  ![Select Color](src/nebula/Images_readme/select_color.png "Selecting a Color")

- **Adjusting Thickness:** Use 'Thickness' to choose the level.  
  ![Thickness](src/nebula/Images_readme/thickness.png "Adjusting Thickness")

- **Adding Headings:** Select 'Heading' and type your text.  
  ![Heading](src/nebula/Images_readme/heading.png "Adding a Heading")

- **Highlighting:** Use 'Highlight' to emphasize areas.  
  ![Highlight](src/nebula/Images_readme/highlight.png "Highlighting Text")

- **Replacing Content:** Use 'Replace' for content modification.  
  ![Replace](src/nebula/Images_readme/replace.png "Replacing Content")

- **Text Searching:** Use 'Search' to find specific text.  
  ![Search](src/nebula/Images_readme/search.png "Searching Text")

- **Undoing Actions:** Click 'Undo' to revert the last action.  
  ![Undo](src/nebula/Images_readme/undo.png "Undoing an Action")

- **Redoing Actions:** Click 'Redo' to reapply an undone action.  
  ![Redo](src/nebula/Images_readme/redo.png "Redoing an Action")

### Settings and Customization
- **Accessing Settings:** Click the settings icon below.  
  ![Settings](src/nebula/Images_readme/settings.png)



### Context Menu
- Interact with any text for AI-based note taking or vulnerability exploitation suggestions.  
  ![Context Menu](src/nebula/Images_readme/context_menu.png)


### Roadmap

- Support more models



### Troubleshooting

Logs are located at `/home/[your_username]/.local/share/nebula/logs`. You would most likely find the reason for the error in one fo those logs

### Get more support

Please open an Issue
---
