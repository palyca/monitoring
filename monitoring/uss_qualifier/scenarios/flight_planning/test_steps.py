import inspect
from datetime import datetime
from typing import List, Union, Optional, Tuple, Iterable, Set, Dict

from uas_standards.astm.f3548.v21.api import OperationalIntentState

from monitoring.monitorlib.fetch import QueryError
from monitoring.monitorlib.scd import bounding_vol4
from monitoring.monitorlib.scd_automated_testing.scd_injection_api import (
    InjectFlightRequest,
    Capability,
    InjectFlightResult,
    InjectFlightResponse,
    DeleteFlightResult,
    DeleteFlightResponse,
)
from monitoring.uss_qualifier.common_data_definitions import Severity
from monitoring.uss_qualifier.resources.flight_planning.flight_intent import (
    FlightIntent,
)
from monitoring.uss_qualifier.resources.flight_planning.flight_planner import (
    FlightPlanner,
)
from monitoring.uss_qualifier.scenarios.scenario import TestScenarioType


def clear_area(
    scenario: TestScenarioType,
    test_step: str,
    flight_intents: List[FlightIntent],
    flight_planners: List[FlightPlanner],
) -> None:
    """Perform a test step to clear the area that will be used in the scenario.

    This function assumes:
    * `scenario` is ready to execute a test step
    * "Area cleared successfully" check declared for specified test step in `scenario`'s documentation

    Args:
      scenario: Scenario in which this step is being executed
      test_step: Name of this test step (according to scenario's documentation)
      flight_intents: Flight intents to be used in this test case (defines bounds of area to be cleared)
      flight_planners: Flight planners to which clear area requests should be issued
    """
    scenario.begin_test_step(test_step)

    volumes = []
    for flight_intent in flight_intents:
        volumes += flight_intent.request.operational_intent.volumes
        volumes += flight_intent.request.operational_intent.off_nominal_volumes
    extent = bounding_vol4(volumes)
    for uss in flight_planners:
        with scenario.check("Area cleared successfully", [uss.participant_id]) as check:
            try:
                resp, query = uss.clear_area(extent)
            except QueryError as e:
                for q in e.queries:
                    scenario.record_query(q)
                check.record_failed(
                    summary=f"Error from {uss.participant_id} when attempting to clear area",
                    severity=Severity.High,
                    details=f"{str(e)}\n\nStack trace:\n{e.stacktrace}",
                    query_timestamps=[q.request.timestamp for q in e.queries],
                )
            scenario.record_query(query)
            if not resp.outcome.success:
                check.record_failed(
                    summary="Area could not be cleared",
                    severity=Severity.High,
                    details=f'Participant indicated "{resp.outcome.message}"',
                    query_timestamps=[query.request.timestamp],
                )

    scenario.end_test_step()


OneOrMoreFlightPlanners = Union[FlightPlanner, List[FlightPlanner]]
OneOrMoreCapabilities = Union[Capability, List[Capability]]


def check_capabilities(
    scenario: TestScenarioType,
    test_step: str,
    required_capabilities: Optional[
        List[Tuple[OneOrMoreFlightPlanners, OneOrMoreCapabilities]]
    ] = None,
    prerequisite_capabilities: Optional[
        List[Tuple[OneOrMoreFlightPlanners, OneOrMoreCapabilities]]
    ] = None,
) -> bool:
    """Perform a check that flight planners support certain capabilities.

    This function assumes:
    * `scenario` is ready to execute a test step
    *  If `required_capabilities` is specified:
      * "Valid responses" check declared for specified test step in `scenario`'s documentation
      * "Support {required_capability}" check declared for specified test in step`scenario`'s documentation

    Args:
      scenario: Scenario in which this step is being executed
      test_step: Name of this test step (according to scenario's documentation)
      required_capabilities: The specified USSs must support these capabilities.
        If a capability is not supported, a "Valid responses" failed check will
        be created.
      prerequisite_capabilities: If any of the specified USSs do not support
        these capabilities, a "Prerequisite capabilities" note will be added and
        the scenario will be indicated to stop, but no failed check will be
        created.
    """
    scenario.begin_test_step(test_step)

    if required_capabilities is None:
        required_capabilities = []
    if prerequisite_capabilities is None:
        prerequisite_capabilities = []

    # Collect all the flight planners that need to be queried
    all_flight_planners: List[FlightPlanner] = []
    for flight_planner_list in [p for p, _ in required_capabilities] + [
        p for p, _ in prerequisite_capabilities
    ]:
        if not isinstance(flight_planner_list, list):
            flight_planner_list = [flight_planner_list]
        for flight_planner in flight_planner_list:
            if flight_planner not in all_flight_planners:
                all_flight_planners.append(flight_planner)

    # Query all the flight planners and collect key results
    flight_planner_capabilities: List[Tuple[FlightPlanner, List[Capability]]] = []
    flight_planner_capability_query_timestamps: List[
        Tuple[FlightPlanner, datetime]
    ] = []
    for flight_planner in all_flight_planners:
        check = scenario.check("Valid responses", [flight_planner.participant_id])
        try:
            uss_info = flight_planner.get_target_information()
            check.record_passed()
        except QueryError as e:
            for q in e.queries:
                scenario.record_query(q)
            check.record_failed(
                summary=f"Failed to query {flight_planner.participant_id} for information",
                severity=Severity.Medium,
                details=f"{str(e)}\n\nStack trace:\n{e.stacktrace}",
                query_timestamps=[q.request.timestamp for q in e.queries],
            )
            continue
        scenario.record_query(uss_info.version_query)
        scenario.record_query(uss_info.capabilities_query)
        flight_planner_capabilities.append((flight_planner, uss_info.capabilities))
        flight_planner_capability_query_timestamps.append(
            (flight_planner, uss_info.capabilities_query.request.timestamp)
        )

    # Check for required capabilities
    for flight_planners, capabilities in required_capabilities:
        if not isinstance(flight_planners, list):
            flight_planners = [flight_planners]
        if not isinstance(capabilities, list):
            capabilities = [capabilities]
        for flight_planner in flight_planners:
            for required_capability in capabilities:
                available_capabilities = [
                    c for p, c in flight_planner_capabilities if p is flight_planner
                ]
                if not available_capabilities:
                    available_capabilities = []
                else:
                    available_capabilities = available_capabilities[0]
                with scenario.check(
                    f"Support {required_capability}", [flight_planner.participant_id]
                ) as check:
                    if required_capability not in available_capabilities:
                        timestamp = [
                            t
                            for p, t in flight_planner_capability_query_timestamps
                            if p is flight_planner
                        ]
                        if timestamp:
                            timestamps = [timestamp[0]]
                        else:
                            timestamps = []
                        check.record_failed(
                            summary=f"Flight planner {flight_planner.participant_id} does not support {required_capability}",
                            severity=Severity.High,
                            details=f"Reported capabilities: ({', '.join(available_capabilities)})",
                            query_timestamps=timestamps,
                        )
                        return False

    # Check for prerequisite capabilities
    unsupported_prerequisites: List[str] = []
    for flight_planners, capabilities in prerequisite_capabilities:
        if not isinstance(flight_planners, list):
            flight_planners = [flight_planners]
        if not isinstance(capabilities, list):
            capabilities = [capabilities]
        for flight_planner in flight_planners:
            available_capabilities = [
                c for p, c in flight_planner_capabilities if p is flight_planner
            ][0]
            unmet_capabilities = ", ".join(
                c for c in capabilities if c not in available_capabilities
            )
            if unmet_capabilities:
                unsupported_prerequisites.append(
                    f"* {flight_planner.participant_id}: {unmet_capabilities}"
                )
    if unsupported_prerequisites:
        scenario.record_note(
            "Unsupported prerequisite capabilities",
            "\n".join(unsupported_prerequisites),
        )
        return False

    scenario.end_test_step()
    return True


def expect_flight_intent_state(
    flight_intent: InjectFlightRequest,
    expected_state: OperationalIntentState,
    scenario: TestScenarioType,
    test_step: str,
) -> None:
    """Confirm that provided flight intent test data has the expected state or raise a ValueError."""
    if flight_intent.operational_intent.state != expected_state:
        function_name = str(inspect.stack()[1][3])
        raise ValueError(
            f"Error in test data: operational intent state for {function_name} during test step '{test_step}' in scenario '{scenario.documentation.name}' is expected to be `Accepted`, but got `{flight_intent.operational_intent.state}` instead"
        )


def plan_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    flight_planner: FlightPlanner,
    flight_intent: InjectFlightRequest,
) -> Tuple[InjectFlightResponse, Optional[str]]:
    """Plan a flight intent that should result in success.

    This function implements the test step described in
    plan_flight_intent.md.

    Returns:
      * The injection response.
      * The ID of the injected flight if it is returned, None otherwise.
    """
    expect_flight_intent_state(
        flight_intent, OperationalIntentState.Accepted, scenario, test_step
    )

    return submit_flight_intent(
        scenario,
        test_step,
        "Successful planning",
        {InjectFlightResult.Planned},
        {InjectFlightResult.Failed: "Failure"},
        flight_planner,
        flight_intent,
    )


def activate_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    flight_planner: FlightPlanner,
    flight_intent: InjectFlightRequest,
    flight_id: Optional[str] = None,
) -> InjectFlightResponse:
    """Activate a flight intent that should result in success.

    This function implements the test step described in
    activate_flight_intent.md.

    Returns: The injection response.
    """
    expect_flight_intent_state(
        flight_intent, OperationalIntentState.Activated, scenario, test_step
    )

    return submit_flight_intent(
        scenario,
        test_step,
        "Successful activation",
        {InjectFlightResult.ReadyToFly},
        {InjectFlightResult.Failed: "Failure"},
        flight_planner,
        flight_intent,
        flight_id,
    )[0]


def modify_planned_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    flight_planner: FlightPlanner,
    flight_intent: InjectFlightRequest,
    flight_id: str,
) -> InjectFlightResponse:
    """Modify a planned flight intent that should result in success.

    This function implements the test step described in
    modify_planned_flight_intent.md.

    Returns: The injection response.
    """
    expect_flight_intent_state(
        flight_intent, OperationalIntentState.Accepted, scenario, test_step
    )

    return submit_flight_intent(
        scenario,
        test_step,
        "Successful modification",
        {InjectFlightResult.Planned},
        {InjectFlightResult.Failed: "Failure"},
        flight_planner,
        flight_intent,
        flight_id,
    )[0]


def modify_activated_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    flight_planner: FlightPlanner,
    flight_intent: InjectFlightRequest,
    flight_id: str,
) -> InjectFlightResponse:
    """Modify an activated flight intent that should result in success.

    This function implements the test step described in
    modify_activated_flight_intent.md.

    Returns: The injection response.
    """
    expect_flight_intent_state(
        flight_intent, OperationalIntentState.Activated, scenario, test_step
    )

    return submit_flight_intent(
        scenario,
        test_step,
        "Successful modification",
        {InjectFlightResult.ReadyToFly},
        {InjectFlightResult.Failed: "Failure"},
        flight_planner,
        flight_intent,
        flight_id,
    )[0]


def submit_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    success_check: str,
    expected_results: Set[InjectFlightResult],
    failed_checks: Dict[InjectFlightResult, str],
    flight_planner: FlightPlanner,
    flight_intent: InjectFlightRequest,
    flight_id: Optional[str] = None,
) -> Tuple[InjectFlightResponse, Optional[str]]:
    """Submit a flight intent with an expected result.
    A check fail is considered of high severity and as such will raise an ScenarioCannotContinueError.

    This function does not directly implement a test step.

    Returns:
      * The injection response.
      * The ID of the injected flight if it is returned, None otherwise.
    """
    scenario.begin_test_step(test_step)
    with scenario.check(success_check, [flight_planner.participant_id]) as check:
        try:
            resp, query, flight_id = flight_planner.request_flight(
                flight_intent, flight_id
            )
        except QueryError as e:
            for q in e.queries:
                scenario.record_query(q)
            check.record_failed(
                summary=f"Error from {flight_planner.participant_id} when attempting to submit a flight intent (flight ID: {flight_id})",
                severity=Severity.High,
                details=f"{str(e)}\n\nStack trace:\n{e.stacktrace}",
                query_timestamps=[q.request.timestamp for q in e.queries],
            )
        scenario.record_query(query)
        notes_suffix = f': "{resp.notes}"' if "notes" in resp and resp.notes else ""

        for unexpected_result, failed_test_check in failed_checks.items():
            with scenario.check(
                failed_test_check, [flight_planner.participant_id]
            ) as specific_failed_check:
                if resp.result == unexpected_result:
                    specific_failed_check.record_failed(
                        summary=f"Flight unexpectedly {resp.result}",
                        severity=Severity.High,
                        details=f'{flight_planner.participant_id} indicated {resp.result} rather than the expected {" or ".join(expected_results)}{notes_suffix}',
                        query_timestamps=[query.request.timestamp],
                    )

        if resp.result in expected_results:
            scenario.end_test_step()
            return resp, flight_id
        else:
            check.record_failed(
                summary=f"Flight unexpectedly {resp.result}",
                severity=Severity.High,
                details=f'{flight_planner.participant_id} indicated {resp.result} rather than the expected {" or ".join(expected_results)}{notes_suffix}',
                query_timestamps=[query.request.timestamp],
            )

    raise RuntimeError(
        "Error with submission of flight intent, but a High Severity issue didn't interrupt execution"
    )


def delete_flight_intent(
    scenario: TestScenarioType,
    test_step: str,
    flight_planner: FlightPlanner,
    flight_id: str,
) -> DeleteFlightResponse:
    """Delete an existing flight intent that should result in success.
    A check fail is considered of high severity and as such will raise an ScenarioCannotContinueError.

    This function implements the test step described in `delete_flight_intent.md`.

    Returns: The deletion response.
    """
    scenario.begin_test_step(test_step)
    with scenario.check(
        "Successful deletion", [flight_planner.participant_id]
    ) as check:
        try:
            resp, query = flight_planner.cleanup_flight(flight_id)
        except QueryError as e:
            for q in e.queries:
                scenario.record_query(q)
            check.record_failed(
                summary=f"Error from {flight_planner.participant_id} when attempting to delete a flight intent (flight ID: {flight_id})",
                severity=Severity.High,
                details=f"{str(e)}\n\nStack trace:\n{e.stacktrace}",
                query_timestamps=[q.request.timestamp for q in e.queries],
            )
        scenario.record_query(query)
        notes_suffix = f': "{resp.notes}"' if "notes" in resp and resp.notes else ""

        if resp.result == DeleteFlightResult.Closed:
            scenario.end_test_step()
            return resp
        else:
            check.record_failed(
                summary=f"Flight deletion attempt unexpectedly {resp.result}",
                severity=Severity.High,
                details=f"{flight_planner.participant_id} indicated {resp.result} rather than the expected {DeleteFlightResult.Closed}{notes_suffix}",
                query_timestamps=[query.request.timestamp],
            )

    raise RuntimeError(
        "Error with deletion of flight intent, but a High Severity issue didn't interrupt execution"
    )


def cleanup_flights(
    scenario: TestScenarioType, flight_planners: Iterable[FlightPlanner]
) -> None:
    """Remove flights during a cleanup test step.

    This function assumes:
    * `scenario` is currently cleaning up (cleanup has started)
    * "Successful flight deletion" check declared for cleanup phase in `scenario`'s documentation
    """
    for flight_planner in flight_planners:
        removed = []
        to_remove = flight_planner.created_flight_ids.copy()
        for flight_id in to_remove:
            with scenario.check(
                "Successful flight deletion", [flight_planner.participant_id]
            ) as check:
                try:
                    resp, query = flight_planner.cleanup_flight(flight_id)
                    scenario.record_query(query)
                except QueryError as e:
                    for q in e.queries:
                        scenario.record_query(q)
                    check.record_failed(
                        summary=f"Failed to clean up flight {flight_id} from {flight_planner.participant_id}",
                        severity=Severity.Medium,
                        details=f"{str(e)}\n\nStack trace:\n{e.stacktrace}",
                        query_timestamps=[q.request.timestamp for q in e.queries],
                    )
                    continue

                if resp.result == DeleteFlightResult.Closed:
                    removed.append(flight_id)
                else:
                    check.record_failed(
                        summary="Failed to delete flight",
                        details=f"USS indicated: {resp.notes}",
                        severity=Severity.Medium,
                        query_timestamps=[query.request.timestamp],
                    )
