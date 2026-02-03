
import json
import os
import uuid
from datetime import datetime
from typing import Optional

class StateManager:
    """Manages the state and history of the orchestration loop."""

    def __init__(
        self,
        logs_root: Optional[str] = None,
        run_id: Optional[str] = None,
        *,
        load_existing: bool = False,
    ):
        """Initializes the StateManager.

        Args:
            logs_root: Root directory where run logs should be written.
                If omitted, defaults to a `logs/` folder under the current working directory.
            run_id: Optional stable run id for reproducibility/testing.
        """
        self.run_id = run_id or str(uuid.uuid4())

        if logs_root is None:
            logs_root = os.path.join(os.getcwd(), "logs")

        self.logs_root = os.path.abspath(logs_root)
        self.log_dir = os.path.join(self.logs_root, self.run_id)
        os.makedirs(self.log_dir, exist_ok=True)
        self.state = {}
        self.history = []
        if load_existing:
            self.load_state()
            self.load_history()

    def update_state(self, key, value):
        """Updates a key in the current state."""
        self.state[key] = value
        self.save_state()

    def get_state(self, key):
        """Retrieves a key from the current state."""
        return self.state.get(key)

    def add_to_history(self, event):
        """Adds an event to the history."""
        timestamp = datetime.now().isoformat()
        self.history.append(f"[{timestamp}] {event}")
        self.save_history()

    def save_state(self):
        """Saves the current state to a file."""
        with open(os.path.join(self.log_dir, "state.json"), "w") as f:
            json.dump(self.state, f, indent=2)

    def save_history(self):
        """Saves the history to a file."""
        with open(os.path.join(self.log_dir, "history.log"), "w") as f:
            f.write("\n".join(self.history))

    def load_state(self):
        """Loads state from disk if present."""
        path = os.path.join(self.log_dir, "state.json")
        try:
            with open(path, "r") as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {}
        except json.JSONDecodeError:
            # Corrupted or mid-write; keep existing in-memory state.
            pass

    def load_history(self):
        """Loads history from disk if present."""
        path = os.path.join(self.log_dir, "history.log")
        try:
            with open(path, "r") as f:
                data = f.read().splitlines()
            self.history = data
        except FileNotFoundError:
            self.history = []
