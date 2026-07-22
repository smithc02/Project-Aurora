"""Command-line entry point for the Project Aurora scaffold."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aurora_core import __version__
from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.hardware.capture_capability import validate_capture_capability
from aurora_core.hardware.capture_device import validate_capture_device
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.capture_modes import validate_capture_modes
from aurora_core.hardware.ddp_output import validate_ddp_output
from aurora_core.hardware.hyperhdr import validate_hyperhdr
from aurora_core.hardware.wled import validate_wled
from aurora_core.runtime import build_runtime_plan
from aurora_core.runtime.errors import AuroraRuntimeError
from aurora_core.runtime.models import ComponentHealthState

APPLICATION_NAME = "Project Aurora"


def build_parser() -> argparse.ArgumentParser:
    """Create the Aurora command-line parser."""
    parser = argparse.ArgumentParser(description="Project Aurora scaffold")
    parser.add_argument(
        "--version", action="store_true", help="Print the version and exit."
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate that the package can start."
    )
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Configuration commands.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    validate_parser = config_subparsers.add_parser(
        "validate", help="Load and validate configuration without connectivity checks."
    )
    validate_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    validate_parser.add_argument("--log-level", help="Override logging.level.")
    runtime_parser = subparsers.add_parser("runtime", help="Runtime planning commands.")
    runtime_subparsers = runtime_parser.add_subparsers(dest="runtime_command")
    plan_parser = runtime_subparsers.add_parser(
        "plan", help="Build a sanitized runtime plan without connectivity checks."
    )
    plan_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    plan_parser.add_argument("--log-level", help="Override logging.level.")
    hardware_parser = subparsers.add_parser(
        "hardware", help="Explicit hardware validation commands."
    )
    hardware_subparsers = hardware_parser.add_subparsers(dest="hardware_command")
    hardware_validate_parser = hardware_subparsers.add_parser(
        "validate", help="Run one read-only hardware validation."
    )
    hardware_validate_subparsers = hardware_validate_parser.add_subparsers(
        dest="hardware_component"
    )
    wled_parser = hardware_validate_subparsers.add_parser(
        "wled", help="Validate WLED using GET /json/info only."
    )
    wled_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    wled_parser.add_argument("--log-level", help="Override logging.level.")
    wled_parser.add_argument(
        "--timeout", type=float, help="Override WLED validation timeout in seconds."
    )
    hyperhdr_parser = hardware_validate_subparsers.add_parser(
        "hyperhdr", help="Validate HyperHDR using GET /json-rpc serverinfo only."
    )
    hyperhdr_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    hyperhdr_parser.add_argument("--log-level", help="Override logging.level.")
    hyperhdr_parser.add_argument(
        "--timeout", type=float, help="Override HyperHDR validation timeout in seconds."
    )
    capture_parser = hardware_validate_subparsers.add_parser(
        "capture-device", help="Validate one Linux V4L2 device node without opening it."
    )
    capture_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    capture_parser.add_argument("--log-level", help="Override logging.level.")
    capture_parser.add_argument(
        "--identifier", help="Override capture_device.identifier."
    )
    capability_parser = hardware_validate_subparsers.add_parser(
        "capture-capability",
        help="Query one Linux V4L2 node with VIDIOC_QUERYCAP only.",
    )
    capability_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    capability_parser.add_argument("--log-level", help="Override logging.level.")
    capability_parser.add_argument(
        "--identifier", help="Override capture_device.identifier."
    )
    modes_parser = hardware_validate_subparsers.add_parser(
        "capture-modes", help="Bounded query-only V4L2 mode enumeration."
    )
    modes_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    modes_parser.add_argument("--log-level", help="Override logging.level.")
    modes_parser.add_argument(
        "--identifier", help="Override capture_device.identifier."
    )
    frame_parser = hardware_validate_subparsers.add_parser(
        "capture-frame", help="Bounded single-frame V4L2 read/write validation."
    )
    frame_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    frame_parser.add_argument("--log-level", help="Override logging.level.")
    frame_parser.add_argument(
        "--identifier", help="Override capture_device.identifier."
    )
    ddp_output_parser = hardware_validate_subparsers.add_parser(
        "ddp-output",
        help="Submit one bounded low-intensity DDP frame and one blackout frame.",
    )
    ddp_output_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    ddp_output_parser.add_argument("--log-level", help="Override logging.level.")
    return parser


def _print_capture_modes_report(report: object) -> None:
    from aurora_core.hardware.models import CaptureModeValidationReport

    assert isinstance(report, CaptureModeValidationReport)
    print(f"Capture mode validation: {report.state.value}")
    print(f"reason: {report.reason_code}")
    print(f"enumeration_complete: {'yes' if report.enumeration_complete else 'no'}")
    print(f"formats_reported: {report.format_count}")
    print(f"frame_sizes_reported: {report.frame_size_count}")
    print(f"frame_intervals_reported: {report.frame_interval_count}")
    for reason in report.partial_reason_codes:
        print(f"partial_reason: {reason}")
    for number, fmt in enumerate(report.formats):
        print(f"\nformat[{number}]")
        print(f"queue_type: {fmt.queue_type}")
        print(f"fourcc: {fmt.fourcc}")
        print(f"big_endian: {'yes' if fmt.big_endian else 'no'}")
        if fmt.description is not None:
            print(f"description: {fmt.description}")
        print(f"compressed: {'yes' if fmt.compressed else 'no'}")
        print(f"emulated: {'yes' if fmt.emulated else 'no'}")
        for size_number, size in enumerate(fmt.frame_sizes):
            if size.kind == "discrete":
                print(f"  size[{size_number}]: {size.width}x{size.height}")
                for interval_number, interval in enumerate(size.intervals):
                    if interval.kind == "discrete":
                        print(
                            f"    interval[{interval_number}]: "
                            f"{interval.numerator}/{interval.denominator} s"
                        )
                        print(
                            f"    fps[{interval_number}]: "
                            f"{interval.denominator}/{interval.numerator}"
                        )
            else:
                print(f"  size[{size_number}]: {size.kind} range")
                print(f"    min_size: {size.min_width}x{size.min_height}")
                print(f"    max_size: {size.max_width}x{size.max_height}")
                if size.kind == "stepwise":
                    print(f"    size_step: {size.step_width}x{size.step_height}")
                print("    frame intervals were not queried")
            for interval_number, interval in enumerate(size.intervals):
                if interval.kind != "discrete":
                    print(f"    interval[{interval_number}]: {interval.kind} range")
                    print(
                        f"      min_interval: {interval.min_numerator}/"
                        f"{interval.min_denominator} s"
                    )
                    print(
                        f"      max_interval: {interval.max_numerator}/"
                        f"{interval.max_denominator} s"
                    )
                    if interval.kind == "stepwise":
                        print(
                            f"      interval_step: {interval.step_numerator}/"
                            f"{interval.step_denominator} s"
                        )
    if not report.device_was_opened:
        print("No capture device was opened.")
        print("No ioctl was issued.")
    elif report.descriptor_was_closed:
        print("The capture device was closed.")
        print(
            f"VIDIOC_QUERYCAP issued: {'yes' if report.querycap_was_issued else 'no'}"
        )
        print(
            "enumeration ioctl issued: "
            f"{'yes' if report.enumeration_ioctl_was_issued else 'no'}"
        )
    else:
        print(
            "The capture device was opened, but descriptor closure could not "
            "be confirmed."
        )
    print(
        "No format was configured, no input was selected, no buffer was allocated, "
        "no stream was started, and no frame was acquired."
    )


def _print_capture_frame_report(report: object) -> None:
    from aurora_core.hardware.models import CaptureFrameValidationReport

    assert isinstance(report, CaptureFrameValidationReport)
    print(f"Capture-frame validation: {report.state.value}")
    print(f"state: {report.state.value}")
    print(f"reason: {report.reason_code}")
    print(f"acquisition_method: {report.acquisition_method or 'none'}")
    print(
        "capability_query_succeeded: "
        f"{'yes' if report.capability_query_succeeded else 'no'}"
    )
    print(
        "current_format_query_succeeded: "
        f"{'yes' if report.current_format_query_succeeded else 'no'}"
    )
    print(f"poll_attempted: {'yes' if report.poll_was_attempted else 'no'}")
    print(f"frame_read_attempted: {'yes' if report.frame_read_was_attempted else 'no'}")
    print(f"frame_received: {'yes' if report.frame_received else 'no'}")
    if report.frame_byte_count is not None:
        print(f"frame_byte_count: {report.frame_byte_count}")
    if report.current_width is not None:
        print(f"current_width: {report.current_width}")
    if report.current_height is not None:
        print(f"current_height: {report.current_height}")
    if report.current_sizeimage is not None:
        print(f"current_sizeimage: {report.current_sizeimage}")
    print(
        "frame_buffer_wipe_confirmed: "
        f"{'yes' if report.frame_buffer_wipe_completed else 'no'}"
    )
    print(
        "descriptor_closure_confirmed: "
        f"{'yes' if report.descriptor_was_closed else 'no'}"
    )
    print(f"cleanup_completed: {'yes' if report.cleanup_completed else 'no'}")
    print(f"streaming_io_was_used: {'yes' if report.streaming_io_was_used else 'no'}")
    print("No frame content was retained, printed, or transmitted.")
    print(
        "One transient userspace frame buffer may have been allocated and wiped. "
        "No V4L2 streaming-buffer negotiation, queueing, mapping, or streaming "
        "ioctl was performed."
    )
    print(
        "HDMI signal, HyperHDR ingest, DDP, WLED output, LEDs, and the "
        "complete lighting path were not tested."
    )


def _print_ddp_output_report(report: object) -> None:
    from aurora_core.hardware.models import DDPOutputValidationReport

    assert isinstance(report, DDPOutputValidationReport)
    print(f"DDP output validation: {report.state.value}")
    print(f"state: {report.state.value}")
    print(f"reason: {report.reason_code}")
    print(
        "led_count: "
        f"{report.led_count if report.led_count is not None else 'not available'}"
    )
    print(f"frame_payload_bytes: {report.frame_payload_bytes}")
    print(f"test_packets_planned: {report.test_packets_planned}")
    print(f"test_packets_sent: {report.test_packets_sent}")
    print(f"blackout_packets_planned: {report.blackout_packets_planned}")
    print(f"blackout_packets_sent: {report.blackout_packets_sent}")
    print(f"test_frame_completed: {'yes' if report.test_frame_completed else 'no'}")
    print(f"blackout_attempted: {'yes' if report.blackout_attempted else 'no'}")
    print(f"blackout_completed: {'yes' if report.blackout_completed else 'no'}")
    print(f"socket_closure_confirmed: {'yes' if report.socket_was_closed else 'no'}")
    print(f"cleanup_completed: {'yes' if report.cleanup_completed else 'no'}")
    print(f"discovery_was_used: {'yes' if report.discovery_was_used else 'no'}")
    print(f"broadcast_was_used: {'yes' if report.broadcast_was_used else 'no'}")
    print(f"multicast_was_used: {'yes' if report.multicast_was_used else 'no'}")
    print(f"retry_was_used: {'yes' if report.retry_was_used else 'no'}")
    _print_ddp_output_safety()


def _print_ddp_output_safety() -> None:
    print(
        "At most one low-intensity DDP test frame and one blackout frame were "
        "attempted. No animation loop, discovery, broadcast, multicast, HTTP "
        "mutation, MQTT, HyperHDR, capture, or runtime-controller operation was "
        "performed."
    )
    print(
        "UDP submission does not verify WLED receipt, LED output, or the complete "
        "lighting path."
    )


def _print_wled_report(report: object) -> None:
    from aurora_core.hardware.models import WLEDValidationReport

    assert isinstance(report, WLEDValidationReport)
    print(f"WLED validation: {report.state.value}")
    if report.state in {ComponentHealthState.HEALTHY, ComponentHealthState.DEGRADED}:
        print(f"firmware_version: {report.firmware_version}")
        print(f"reported_led_count: {report.reported_led_count}")
        expected = (
            "not configured"
            if report.expected_led_count is None
            else str(report.expected_led_count)
        )
        print(f"expected_led_count: {expected}")
        match = (
            "not evaluated"
            if report.led_count_matches is None
            else ("yes" if report.led_count_matches else "no")
        )
        print(f"led_count_match: {match}")
        print("Read-only validation completed; no WLED state was changed.")
        print("HyperHDR, capture, DDP, and the complete lighting path were not tested.")
    else:
        print(f"reason: {report.reason_code}")
        print("No WLED state was changed.")


def _print_hyperhdr_report(report: object) -> None:
    from aurora_core.hardware.models import HyperHDRValidationReport

    assert isinstance(report, HyperHDRValidationReport)
    print(f"HyperHDR validation: {report.state.value}")
    if report.state is ComponentHealthState.HEALTHY:
        print("serverinfo_response: valid")
        hdr_mode = (
            "not reported"
            if report.hdr_mode_enabled is None
            else ("enabled" if report.hdr_mode_enabled else "disabled")
        )
        print(f"hdr_mode: {hdr_mode}")
        print("Read-only validation completed; no HyperHDR state was changed.")
        print(
            "Capture input, WLED output, DDP, and the complete lighting path "
            "were not tested."
        )
    elif report.state is ComponentHealthState.DISABLED:
        print(f"reason: {report.reason_code}")
        print("No HyperHDR request was made.")
        print("No HyperHDR state was changed.")
    else:
        print(f"reason: {report.reason_code}")
        print("No HyperHDR state was changed.")


def _print_capture_device_report(report: object) -> None:
    from aurora_core.hardware.models import CaptureDeviceValidationReport

    assert isinstance(report, CaptureDeviceValidationReport)
    print(f"Capture-device validation: {report.state.value}")
    if report.state is ComponentHealthState.HEALTHY:
        print("device_node: present")
        print("device_type: character")
        print("v4l2_registration: present")
        print("process_read_access: yes")
        print(f"device_name: {report.device_name}")
        print("Non-invasive validation completed; the device was not opened.")
        print(
            "Capture capability was not queried. Formats, frames, signal, HyperHDR "
            "ingest, DDP, WLED output, and the complete lighting path were not tested."
        )
    elif report.state is ComponentHealthState.DISABLED:
        print(f"reason: {report.reason_code}")
        print("No capture-device probe was performed.")
        print("The capture device was not opened.")
    else:
        print(f"reason: {report.reason_code}")
        print("The capture device was not opened.")


def _print_capture_capability_report(report: object) -> None:
    from aurora_core.hardware.models import CaptureCapabilityValidationReport

    assert isinstance(report, CaptureCapabilityValidationReport)
    print(f"Capture capability validation: {report.state.value}")
    if report.state is ComponentHealthState.HEALTHY:
        print("querycap_response: valid")
        print(f"driver: {report.driver_name}")
        print(f"card: {report.card_name}")
        print(f"v4l2_api_version: {report.v4l2_api_version}")
        print(
            f"single_planar_capture: {'yes' if report.single_planar_capture else 'no'}"
        )
        print(f"multi_planar_capture: {'yes' if report.multi_planar_capture else 'no'}")
        print(f"streaming_io: {'yes' if report.streaming_io else 'no'}")
        print(f"readwrite_io: {'yes' if report.readwrite_io else 'no'}")
        print("Query-only validation completed.")
        print("The device was opened only for VIDIOC_QUERYCAP and was then closed.")
        print(
            "No format was configured, no buffer was allocated, and no frame "
            "was acquired."
        )
        print(
            "Formats, resolutions, frame rates, HDMI signal, HyperHDR ingest, "
            "DDP, WLED output, LEDs, and the complete lighting path were not tested."
        )
    elif report.state is ComponentHealthState.DISABLED:
        print(f"reason: {report.reason_code}")
        print("No capture device was opened.")
        print("No ioctl was issued.")
    else:
        print(f"reason: {report.reason_code}")
        if not report.device_was_opened:
            print("No capture device was opened.")
            print("No ioctl was issued.")
        else:
            assert report.descriptor_was_closed
            print("The capture device was closed.")
            if report.ioctl_was_issued:
                print("VIDIOC_QUERYCAP was issued.")
            else:
                print("No ioctl was issued.")
            print("No frame was acquired and no capture settings were changed.")


def main() -> int:
    """Run the Project Aurora command-line interface."""
    args = build_parser().parse_args()
    if args.version:
        print(f"{APPLICATION_NAME} {__version__}")
        return 0
    if args.check:
        print(f"{APPLICATION_NAME} {__version__}: package startup check passed")
        return 0
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "ddp-output"
    ):
        ddp_override: dict[str, object] = {}
        if args.log_level is not None:
            ddp_override["logging"] = {"level": args.log_level}
        try:
            ddp_report = validate_ddp_output(
                load_settings(
                    config_path=args.config, cli_overrides=ddp_override or None
                )
            )
        except AuroraConfigurationError:
            print("DDP output validation configuration failed.", file=sys.stderr)
            _print_ddp_output_safety()
            return 1
        _print_ddp_output_report(ddp_report)
        return 0 if ddp_report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "capture-frame"
    ):
        frame_override: dict[str, object] = {}
        if args.log_level is not None:
            frame_override["logging"] = {"level": args.log_level}
        if args.identifier is not None:
            frame_override["capture_device"] = {"identifier": args.identifier}
        try:
            frame_report = validate_capture_frame(
                load_settings(
                    config_path=args.config, cli_overrides=frame_override or None
                )
            )
        except AuroraConfigurationError:
            print("Capture-frame validation configuration failed.", file=sys.stderr)
            return 1
        _print_capture_frame_report(frame_report)
        return 0 if frame_report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "wled"
    ):
        wled_override: dict[str, object] = {}
        if args.log_level is not None:
            wled_override["logging"] = {"level": args.log_level}
        if args.timeout is not None:
            wled_override["wled"] = {"validation_timeout_seconds": args.timeout}
        try:
            report = validate_wled(
                load_settings(
                    config_path=args.config, cli_overrides=wled_override or None
                )
            )
        except AuroraConfigurationError as error:
            print(f"WLED validation configuration failed: {error}", file=sys.stderr)
            return 1
        _print_wled_report(report)
        return 0 if report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "hyperhdr"
    ):
        hyperhdr_override: dict[str, object] = {}
        if args.log_level is not None:
            hyperhdr_override["logging"] = {"level": args.log_level}
        if args.timeout is not None:
            hyperhdr_override["hyperhdr"] = {"validation_timeout_seconds": args.timeout}
        try:
            hyperhdr_report = validate_hyperhdr(
                load_settings(
                    config_path=args.config, cli_overrides=hyperhdr_override or None
                )
            )
        except AuroraConfigurationError as error:
            print(f"HyperHDR validation configuration failed: {error}", file=sys.stderr)
            return 1
        _print_hyperhdr_report(hyperhdr_report)
        return 0 if hyperhdr_report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "capture-modes"
    ):
        override: dict[str, object] = {}
        if args.log_level is not None:
            override["logging"] = {"level": args.log_level}
        if args.identifier is not None:
            override["capture_device"] = {"identifier": args.identifier}
        try:
            mode_report = validate_capture_modes(
                load_settings(config_path=args.config, cli_overrides=override or None)
            )
        except AuroraConfigurationError:
            print("Capture mode validation configuration failed.", file=sys.stderr)
            return 1
        _print_capture_modes_report(mode_report)
        return 0 if mode_report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "capture-device"
    ):
        capture_override: dict[str, object] = {}
        if args.log_level is not None:
            capture_override["logging"] = {"level": args.log_level}
        if args.identifier is not None:
            capture_override["capture_device"] = {"identifier": args.identifier}
        try:
            capture_report = validate_capture_device(
                load_settings(
                    config_path=args.config, cli_overrides=capture_override or None
                )
            )
        except AuroraConfigurationError:
            print("Capture-device validation configuration failed.", file=sys.stderr)
            return 1
        _print_capture_device_report(capture_report)
        return 0 if capture_report.state is ComponentHealthState.HEALTHY else 1
    if (
        args.command == "hardware"
        and args.hardware_command == "validate"
        and args.hardware_component == "capture-capability"
    ):
        capability_override: dict[str, object] = {}
        if args.log_level is not None:
            capability_override["logging"] = {"level": args.log_level}
        if args.identifier is not None:
            capability_override["capture_device"] = {"identifier": args.identifier}
        try:
            capability_report = validate_capture_capability(
                load_settings(
                    config_path=args.config, cli_overrides=capability_override or None
                )
            )
        except AuroraConfigurationError:
            print(
                "Capture capability validation configuration failed.", file=sys.stderr
            )
            return 1
        _print_capture_capability_report(capability_report)
        return 0 if capability_report.state is ComponentHealthState.HEALTHY else 1
    if args.command == "config" and args.config_command == "validate":
        overrides = (
            {"logging": {"level": args.log_level}}
            if args.log_level is not None
            else None
        )
        try:
            load_settings(config_path=args.config, cli_overrides=overrides)
        except AuroraConfigurationError as error:
            print(f"Configuration validation failed: {error}", file=sys.stderr)
            return 1
        print("Configuration is valid (connectivity was not tested).")
        return 0
    if args.command == "runtime" and args.runtime_command == "plan":
        overrides = (
            {"logging": {"level": args.log_level}}
            if args.log_level is not None
            else None
        )
        try:
            plan = build_runtime_plan(
                load_settings(config_path=args.config, cli_overrides=overrides)
            )
        except (AuroraConfigurationError, AuroraRuntimeError) as error:
            print(f"Runtime planning failed: {error}", file=sys.stderr)
            return 1
        print("Runtime plan valid; connectivity and hardware were not tested.")
        for component in plan.components:
            if component.enabled:
                print(f"{component.component_id.value}: configured, health unknown")
            else:
                print(f"{component.component_id.value}: disabled")
        print(f"lighting_zones: {plan.lighting_zone_count} configured")
        layout = "configured" if plan.led_layout_configured else "not configured"
        print(f"led_layout: {layout}")
        return 0
    print(f"{APPLICATION_NAME} {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
