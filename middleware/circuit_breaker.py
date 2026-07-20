from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
import threading

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, timeout: int = 60, half_open_max_calls: int = 3):
        self.failure_counts = defaultdict(int)
        self.last_failure_time = {}
        self.circuit_states = defaultdict(lambda: CircuitState.CLOSED)
        self.half_open_calls = defaultdict(int)
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.half_open_max_calls = half_open_max_calls
        self._lock = threading.RLock()

    def is_open(self, service_name: str) -> bool:
        """Check if circuit is open (preventing requests)"""
        with self._lock:
            current_state = self.circuit_states[service_name]

            # If circuit is open, check if timeout has passed to move to half-open
            if current_state == CircuitState.OPEN:
                if service_name in self.last_failure_time:
                    time_since_failure = datetime.now(timezone.utc) - self.last_failure_time[service_name]
                    if time_since_failure > timedelta(seconds=self.timeout):
                        # Transition to half-open state
                        self.circuit_states[service_name] = CircuitState.HALF_OPEN
                        self.half_open_calls[service_name] = 0
                        return False
                return True

            # Half-open state: allow limited requests to test service
            elif current_state == CircuitState.HALF_OPEN:
                if self.half_open_calls[service_name] >= self.half_open_max_calls:
                    return True
                return False

            # Closed state: normal operation
            return False

    def record_failure(self, service_name: str):
        """Record a failure"""
        with self._lock:
            current_state = self.circuit_states[service_name]
            self.failure_counts[service_name] += 1
            self.last_failure_time[service_name] = datetime.now(timezone.utc)

            # If in half-open and failure occurs, immediately go back to open
            if current_state == CircuitState.HALF_OPEN:
                self.circuit_states[service_name] = CircuitState.OPEN
                self.half_open_calls[service_name] = 0
            # If failures exceed threshold, open the circuit
            elif self.failure_counts[service_name] >= self.failure_threshold:
                self.circuit_states[service_name] = CircuitState.OPEN

    def record_success(self, service_name: str):
        """Record a success"""
        with self._lock:
            current_state = self.circuit_states[service_name]

            # If in half-open state, increment successful calls
            if current_state == CircuitState.HALF_OPEN:
                self.half_open_calls[service_name] += 1
                # If enough successful calls, close the circuit
                if self.half_open_calls[service_name] >= self.half_open_max_calls:
                    self.circuit_states[service_name] = CircuitState.CLOSED
                    self.failure_counts[service_name] = 0
                    self.half_open_calls[service_name] = 0
                    if service_name in self.last_failure_time:
                        del self.last_failure_time[service_name]
            # If in closed state, just reset the failure count
            else:
                self.failure_counts[service_name] = 0
                if service_name in self.last_failure_time:
                    del self.last_failure_time[service_name]

    def get_state(self, service_name: str) -> CircuitState:
        """Get current circuit state for a service"""
        with self._lock:
            return self.circuit_states[service_name]

circuit_breaker = CircuitBreaker()
