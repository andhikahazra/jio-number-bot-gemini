import requests
import json
import threading
from typing import Optional, Tuple, List, Dict, Any

API_BASE_URL = "https://api.grizzlysms.com/stubs/handler_api.php"

SERVICE_ALIASES = {
    "myjio": "jio",
    "jio": "jio",
    "jii": "jii",
}

class GrizzlySMSClient:
    """
    Thread-safe HTTP client for Grizzly SMS API.
    Supports balance queries, number acquisition, status updating, and active activation queries.
    """
    def __init__(self, api_key: str, timeout: int = 60):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; GrizzlyMyJioClient/2.0)"
        })
        self._lock = threading.RLock()

    def set_api_key(self, api_key: str):
        with self._lock:
            self.api_key = api_key.strip()

    def _request(self, action: str, params: Optional[dict] = None) -> Tuple[str, Optional[str]]:
        """
        Send API request to Grizzly SMS.
        Returns (raw_response, error_msg_if_any)
        """
        with self._lock:
            api_key = self.api_key

        if not api_key:
            return "NO_KEY", "Grizzly SMS API key is not configured."

        query = {"api_key": api_key, "action": action}
        if params:
            query.update(params)

        try:
            resp = self.session.get(API_BASE_URL, params=query, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text.strip(), None
        except requests.exceptions.Timeout:
            return "ERROR_TIMEOUT", f"Network request timed out after {self.timeout}s."
        except requests.RequestException as e:
            return "ERROR_NETWORK", f"Network request failed: {e}"

    def get_balance(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Fetch account balance in USD.
        Returns (balance, error_message)
        """
        res, err = self._request("getBalance")
        if err:
            return None, err
        if res.startswith("ACCESS_BALANCE:"):
            try:
                return float(res.split(":", 1)[1]), None
            except ValueError:
                return None, f"Invalid balance response: {res}"
        elif res == "BAD_KEY":
            return None, "Invalid API Key! Please verify your Grizzly SMS API key."
        return None, f"Unexpected balance response: {res}"

    def request_number(
        self,
        service: str = "jio",
        country: str = "22",
        max_price: Optional[float] = 0.3,
    ) -> Tuple[Optional[str], Optional[str], str, Optional[str]]:
        """
        Request a virtual number.
        Returns (activation_id, phone_number, raw_response, error_message)
        """
        service_code = SERVICE_ALIASES.get(service.lower().strip(), service.strip())
        params = {"service": service_code, "country": str(country)}
        if max_price is not None and max_price > 0:
            params["maxPrice"] = str(max_price)

        res, err = self._request("getNumber", params)
        if err:
            return None, None, res, err

        if res.startswith("ACCESS_NUMBER:"):
            parts = res.split(":")
            if len(parts) >= 3:
                act_id = parts[1]
                phone_num = parts[2]
                return act_id, phone_num, res, None
        return None, None, res, None

    def set_status(self, activation_id: str, status: int) -> Tuple[str, Optional[str]]:
        """
        Status codes:
        1: Number ready (SMS sent / waiting)
        3: Request another code
        6: Finish activation (Success)
        8: Cancel activation (Refund)
        """
        return self._request("setStatus", {"id": str(activation_id), "status": str(status)})

    def get_status(self, activation_id: str) -> Tuple[str, Optional[str]]:
        """
        Check activation status:
        STATUS_WAIT_CODE: waiting for SMS
        STATUS_OK:<code>: SMS received
        STATUS_CANCEL: cancelled
        """
        return self._request("getStatus", {"id": str(activation_id)})

    def get_active_activations(self) -> List[Dict[str, Any]]:
        """
        Query all active activations currently running on the account.
        """
        res, err = self._request("getActiveActivations")
        if err or not res or res in ("NO_ACTIVATIONS", "BAD_KEY", "NO_KEY", "ERROR_NETWORK", "ERROR_TIMEOUT"):
            return []
        try:
            data = json.loads(res)
            raw_items = []
            if isinstance(data, dict):
                if data.get("status") == "success" and "activeAct" in data:
                    raw_items = data["activeAct"]
                elif "activeAct" in data:
                    raw_items = data["activeAct"]
            elif isinstance(data, list):
                raw_items = data

            results = []
            for item in raw_items:
                if isinstance(item, dict):
                    act_id = str(item.get("activationId") or item.get("id") or "")
                    phone = str(item.get("phone") or item.get("phoneNumber") or "")
                    service = str(item.get("service") or "")
                    if act_id and phone:
                        results.append({
                            "activation_id": act_id,
                            "phone_number": phone,
                            "service": service,
                        })
            return results
        except Exception:
            return []
