import os

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtGui import QFont  # Use QtGui in PyQt6
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLineEdit,
                             QMainWindow, QMessageBox, QPushButton,
                             QScrollArea, QSizePolicy, QSpacerItem,
                             QVBoxLayout, QWidget)

from .update_utils import return_path


class ToolsWindow(QMainWindow):
    def __init__(
        self,
        available_tools,
        selected_tools,
        icons_path,
        update_callback,
        add_tool_callback,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("configWindow")
        self.setWindowTitle("Select Tools")
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #1E1E1E; 
                font-family: Courier
            }
            QPushButton:hover {
        border: 2px solid#333333; /* Highlight border on hover with VSCode blue */
    }
            QPushButton {
                color: #D4D4D4; /* Light grey text */
                font-size: 12px; /* Slightly smaller font */
                background-color: #1E1E1E; /* Darker button background */
                border: 1px solid #333333;; /* Subtle border */
                border-radius: 4px; /* Less rounded corners */
                padding: 6px 12px; /* Adjust padding */
                margin: 4px;
                text-align: left; /* Align text to left like VSCode buttons */
                font-family: Courier
            }
            QPushButton:checked {
                background-color:#007ACC; /* VSCode selection color */
                color: white; /* White text for checked state */
            }
            QPushButton:hover {
                border: 1px solid#007ACC; /* Highlight border on hover */
            }
            QPushButton:pressed {
                background-color: #007ACC /* Darker shade when pressed */
            }
            QLineEdit {
                padding: 5px;
                margin: 5px;
                border: 1px solid #333333; /* More subtle border */
                border-radius: 4px; /* Consistent border radius */
                color: #D4D4D4; /* Light grey text */
                background-color: #333333; /* Matching the button background */
                font-family: Courier
            }
            QScrollArea {
                border: none; /* Remove border from scroll areas if present */
            }
        """
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        self.selected_tools = selected_tools

        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search for a tool...")
        self.search_field.textChanged.connect(
            self.search_tool
        )  # This connects the signal to the method

        layout.addWidget(self.search_field)

        add_tool_layout = QHBoxLayout()
        self.tool_name_input = QLineEdit()
        self.tool_name_input.setPlaceholderText("Enter new tool name...")
        font = QFont("Courier", 12)  # Specify the font name and font size
        self.tool_name_input.setFont(font)
        self.tool_name_input.setFixedHeight(40)  # Width = 200, Height = 40
        self.search_field.setFont(font)
        add_tool_button = QPushButton()
        add_tool_button.setIcon(QIcon(return_path("Images/add_tool.png")))
        add_tool_button.clicked.connect(
            lambda: self.provide_feedback_and_execute(
                add_tool_button,
                return_path("Images/clicked.png"),
                return_path("Images/add_tool.png"),
                self.add_tool,
            )
        )
        add_tool_button.setFont(font)
        add_tool_button.setFixedHeight(40)
        add_tool_layout.addWidget(self.tool_name_input)
        add_tool_layout.addWidget(add_tool_button)
        layout.addLayout(add_tool_layout)
        select_deselect_layout = QHBoxLayout()
        self.select_all_button = QPushButton("Select All")
        self.select_all_button.clicked.connect(self.select_all_tools)
        self.deselect_all_button = QPushButton("Deselect All")
        self.deselect_all_button.clicked.connect(self.deselect_all_tools)

        # Add buttons to the layout and set their properties if needed
        select_deselect_layout.addWidget(self.select_all_button)
        select_deselect_layout.addWidget(self.deselect_all_button)
        select_deselect_layout.addItem(
            QSpacerItem(
                40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
            )
        )

        # Add the new layout to the main layout
        layout.addLayout(select_deselect_layout)
        container_widget = QWidget()
        self.grid_layout = QGridLayout(container_widget)

        self.buttons = {}  # Store buttons with tool names as keys
        self.available_tools = available_tools
        self.icons_path = icons_path
        self.update_callback = update_callback
        self.add_tool_callback = add_tool_callback

        row = col = 0
        max_col = 4  # Adjust based on preference
        for tool_name in self.available_tools:
            icon_file = os.path.join(icons_path, f"{tool_name}.png")
            button = QPushButton(QIcon(icon_file), tool_name)
            button.setCheckable(True)
            button.setIconSize(QSize(64, 64))
            if tool_name in selected_tools:
                button.setChecked(True)
            button.toggled.connect(
                lambda checked, name=tool_name: self.tool_selection_changed(
                    name, checked
                )
            )

            self.buttons[tool_name] = button
            self.grid_layout.addWidget(button, row, col)
            col += 1
            if col >= max_col:
                row += 1
                col = 0

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(container_widget)
        layout.addWidget(self.scroll_area)
        self.resize(1200, 600)
        self.center()

    def select_all_tools(self):
        for tool_name, button in self.buttons.items():
            if not button.isChecked():
                button.setChecked(True)
                if tool_name not in self.selected_tools:
                    self.selected_tools.append(tool_name)
        self.update_callback(self.selected_tools)

    def update_config(self, available_tools, selected_tools):
        self.available_tools = available_tools
        self.selected_tools = selected_tools
        self.refresh_tools_grid()  # Refresh the UI to reflect changes

    # Add the deselect_all_tools method to ToolsWindow class
    def deselect_all_tools(self):
        for tool_name, button in self.buttons.items():
            if button.isChecked():
                button.setChecked(False)
                if tool_name in self.selected_tools:
                    self.selected_tools.remove(tool_name)
        self.update_callback(self.selected_tools)

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def change_icon_temporarily(
        self, action, temp_icon_path, original_icon_path, delay=500
    ):
        action.setIcon(QIcon(temp_icon_path))
        self.window().repaint()
        QApplication.processEvents()  # Force the UI to update
        QTimer.singleShot(delay, lambda: action.setIcon(QIcon(original_icon_path)))

    def provide_feedback_and_execute(
        self, action, temp_icon_path, original_icon_path, function
    ):
        self.change_icon_temporarily(action, temp_icon_path, original_icon_path)
        function()

    def tool_selection_changed(self, tool_name, checked):
        if checked and tool_name not in self.selected_tools:
            self.selected_tools.append(tool_name)
        elif not checked and tool_name in self.selected_tools:
            self.selected_tools.remove(tool_name)
        self.update_callback(self.selected_tools)

    def add_tool(self):
        tool_name = self.tool_name_input.text().strip()
        if tool_name and tool_name not in self.available_tools:
            self.add_tool_callback(
                tool_name
            )  # Update the main configuration with the new tool
            self.available_tools.sort()
            self.refresh_tools_grid()  # Refresh the grid to include the new tool in sorted order
            self.tool_name_input.clear()
            QMessageBox.information(
                self, "Tool Added", f"'{tool_name}' has been added successfully."
            )

        else:
            QMessageBox.warning(
                self, "Tool Exists", f"The tool '{tool_name}' already exists."
            )

    def refresh_tools_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.available_tools.sort()  # This line ensures tools are sorted alphabetically

        row = col = 0
        max_col = 4  # Adjust based on preference
        for tool_name in self.available_tools:
            icon_path = os.path.join(self.icons_path, f"{tool_name}.png")
            if not os.path.exists(icon_path):
                icon_path = "path/to/default/icon.png"  # Provide a default icon path

            button = QPushButton(QIcon(icon_path), tool_name)
            button.setCheckable(True)
            button.setIconSize(QSize(64, 64))
            button.setChecked(tool_name in self.selected_tools)
            button.toggled.connect(
                lambda checked, name=tool_name: self.tool_selection_changed(
                    name, checked
                )
            )
            self.buttons[tool_name] = button
            self.grid_layout.addWidget(button, row, col)
            col += 1
            if col >= max_col:
                row += 1
                col = 0
        self.window().repaint()

    def search_tool(self, text):
        text = text.lower()
        matches = []  # Store tuples of (index, tool_name, button)

        for tool_name, button in self.buttons.items():
            lower_tool_name = tool_name.lower()
            index = lower_tool_name.find(text)
            if index != -1:
                matches.append((index, tool_name, button))
                button.setStyleSheet(
                    "QPushButton { border: 3px solid#333333; }"
                )  # Highlight matched button
            else:
                button.setStyleSheet("")

        matches.sort(key=lambda x: x[0])

        if matches:
            self.scrollToButton(matches[0][2])

    def scrollToButton(self, button):
        button_pos = button.pos()

        vertical_pos = button_pos.y()

        scroll_position = int(vertical_pos - self.scroll_area.viewport().height() / 2)

        self.scroll_area.verticalScrollBar().setValue(scroll_position)
