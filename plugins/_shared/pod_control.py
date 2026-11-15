"""Stops or terminates the RunPod pod this WanGP runs in.

RunPod gives every pod its id (RUNPOD_POD_ID), an API key limited to that pod
(RUNPOD_API_KEY) and the runpodctl command line tool. Kept free of Gradio and
WanGP imports so that it can be tested on its own.
"""
import json
import os
import shutil
import subprocess
import urllib.request

KEEP, STOP, TERMINATE = "keep", "stop", "terminate"
CHOICES = [
    ("Leave the pod running", KEEP),
    ("Stop the pod (not possible when it has a network volume)", STOP),
    ("Terminate the pod: it is DELETED, only a network volume survives", TERMINATE),
]
GRAPHQL_URL = "https://api.runpod.io/graphql"
MUTATIONS = {STOP: "podStop", TERMINATE: "podTerminate"}
RUNPODCTL = {STOP: ["stop", "pod"], TERMINATE: ["remove", "pod"]}


def pod_id():
    return (os.environ.get("RUNPOD_POD_ID") or "").strip()


def unavailable_reason():
    """Why the pod cannot be shut down from here, or an empty string."""
    if not pod_id():
        return "this WanGP does not run in a RunPod pod (RUNPOD_POD_ID is not set)"
    if not shutil.which("runpodctl") and not os.environ.get("RUNPOD_API_KEY"):
        return "neither runpodctl nor RUNPOD_API_KEY is available in this pod"
    return ""


def _with_runpodctl(action):
    tool = shutil.which("runpodctl")
    if not tool:
        return False, "runpodctl is not installed"
    done = subprocess.run([tool, *RUNPODCTL[action], pod_id()], capture_output=True, text=True, timeout=120)
    output = " ".join((done.stdout + " " + done.stderr).split())
    return done.returncode == 0, output or f"runpodctl exited with code {done.returncode}"


def _with_api(action):
    key = (os.environ.get("RUNPOD_API_KEY") or "").strip()
    if not key:
        return False, "RUNPOD_API_KEY is not set"
    name = MUTATIONS[action]
    selection = "" if action == TERMINATE else " { id desiredStatus }"
    query = "mutation { %s(input: {podId: %s})%s }" % (name, json.dumps(pod_id()), selection)
    request = urllib.request.Request(GRAPHQL_URL, data=json.dumps({"query": query}).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # network errors, HTTP errors, invalid JSON
        return False, f"RunPod API: {error}"
    if answer.get("errors"):
        return False, "RunPod API: " + "; ".join(str(e.get("message", e)) for e in answer["errors"])
    return True, "RunPod API accepted the request"


def shutdown(action):
    """Stops or terminates this pod. Returns (ok, message); on success the pod goes away shortly after."""
    if action not in MUTATIONS:
        return False, f"Unknown action: {action}"
    reason = unavailable_reason()
    if reason:
        return False, f"The pod was left running: {reason}."
    attempts = []
    for method in (_with_runpodctl, _with_api):
        try:
            ok, message = method(action)
        except Exception as error:
            ok, message = False, str(error)
        if ok:
            return True, message
        attempts.append(message)
    return False, "The pod was left running: " + " / ".join(attempts)
