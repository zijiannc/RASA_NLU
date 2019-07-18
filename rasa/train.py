import asyncio
import os
import tempfile
from contextlib import ExitStack
from typing import Text, Optional, List, Union, Dict

from rasa import model, data
from rasa.core.domain import Domain, InvalidDomain
from rasa.model import Fingerprint, should_retrain
from rasa.skill import SkillSelector
from rasa.utils.common import TempDirectoryPath

from rasa.cli.utils import (
    create_output_path,
    print_success,
    print_warning,
    print_error,
    bcolors,
    print_color,
)
from rasa.constants import DEFAULT_MODELS_PATH


def train(
    domain: Text,
    config: Text,
    training_files: Union[Text, List[Text]],
    output: Text = DEFAULT_MODELS_PATH,
    force_training: bool = False,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
) -> Optional[Text]:
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(
        train_async(
            domain=domain,
            config=config,
            training_files=training_files,
            output_path=output,
            force_training=force_training,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )
    )


async def train_async(
    domain: Union[Domain, Text],
    config: Text,
    training_files: Optional[Union[Text, List[Text]]],
    output_path: Text = DEFAULT_MODELS_PATH,
    force_training: bool = False,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
) -> Optional[Text]:
    """Trains a Rasa model (Core and NLU).

    Args:
        domain: Path to the domain file.
        config: Path to the config for Core and NLU.
        training_files: Paths to the training data for Core and NLU.
        output_path: Output path.
        force_training: If `True` retrain model even if data has not changed.
        fixed_model_name: Name of model to be stored.
        kwargs: Additional training parameters.

    Returns:
        Path of the trained model archive.
    """
    skill_imports = SkillSelector.load(config, training_files)
    try:
        domain = Domain.load(domain, skill_imports)
        domain.check_missing_templates()
    except InvalidDomain:
        domain = None

    story_directory, nlu_data_directory = data.get_core_nlu_directories(
        training_files, skill_imports
    )

    with ExitStack() as stack:
        train_path = stack.enter_context(TempDirectoryPath(tempfile.mkdtemp()))
        nlu_data = stack.enter_context(TempDirectoryPath(nlu_data_directory))
        story = stack.enter_context(TempDirectoryPath(story_directory))

        if domain is None:
            return handle_domain_if_not_exists(
                config, nlu_data_directory, output_path, fixed_model_name
            )

        return await _train_async_internal(
            domain,
            config,
            train_path,
            nlu_data,
            story,
            output_path,
            force_training,
            fixed_model_name,
            kwargs,
        )

    if domain is None:
        return handle_domain_if_not_exists(
            config, nlu_data_directory, output_path, fixed_model_name
        )


def handle_domain_if_not_exists(
    config, nlu_data_directory, output_path, fixed_model_name
):
    nlu_model_only = _train_nlu_with_validated_data(
        config=config,
        nlu_data_directory=nlu_data_directory,
        output=output_path,
        fixed_model_name=fixed_model_name,
    )
    print_warning(
        "Core training was skipped because no valid domain file was found. Only an nlu-model was created."
        "Please specify a valid domain using '--domain' argument or check if the provided domain file exists."
    )
    return nlu_model_only


async def _train_async_internal(
    domain: Union[Domain, Text],
    config: Text,
    train_path: Text,
    nlu_data_directory: Text,
    story_directory: Text,
    output_path: Text,
    force_training: bool,
    fixed_model_name: Optional[Text],
    kwargs: Optional[Dict],
) -> Optional[Text]:
    """Trains a Rasa model (Core and NLU). Use only from `train_async`.

    Args:
        domain: Path to the domain file.
        config: Path to the config for Core and NLU.
        train_path: Directory in which to train the model.
        nlu_data_directory: Path to NLU training files.
        story_directory: Path to Core training files.
        output_path: Output path.
        force_training: If `True` retrain model even if data has not changed.
        fixed_model_name: Name of model to be stored.
        kwargs: Additional training parameters.

    Returns:
        Path of the trained model archive.
    """
    new_fingerprint = model.model_fingerprint(
        config, domain, nlu_data_directory, story_directory
    )

    dialogue_data_not_present = not os.listdir(story_directory)
    nlu_data_not_present = not os.listdir(nlu_data_directory)

    if dialogue_data_not_present and nlu_data_not_present:
        print_error(
            "No training data given. Please provide stories and NLU data in "
            "order to train a Rasa model using the '--data' argument."
        )
        return

    if dialogue_data_not_present:
        print_warning(
            "No dialogue data present. Just a Rasa NLU model will be trained."
        )
        return _train_nlu_with_validated_data(
            config=config,
            nlu_data_directory=nlu_data_directory,
            output=output_path,
            fixed_model_name=fixed_model_name,
        )

    if nlu_data_not_present:
        print_warning("No NLU data present. Just a Rasa Core model will be trained.")
        return await _train_core_with_validated_data(
            domain=domain,
            config=config,
            story_directory=story_directory,
            output=output_path,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )

    old_model = model.get_latest_model(output_path)
    retrain_core, retrain_nlu = should_retrain(new_fingerprint, old_model, train_path)

    if force_training or retrain_core or retrain_nlu:
        await _do_training(
            domain=domain,
            config=config,
            output_path=output_path,
            train_path=train_path,
            nlu_data_directory=nlu_data_directory,
            story_directory=story_directory,
            force_training=force_training,
            retrain_core=retrain_core,
            retrain_nlu=retrain_nlu,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )

        return _package_model(
            new_fingerprint=new_fingerprint,
            output_path=output_path,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
        )

    print_success(
        "Nothing changed. You can use the old model stored at '{}'."
        "".format(os.path.abspath(old_model))
    )
    return old_model


async def _do_training(
    domain: Union[Domain, Text],
    config: Text,
    nlu_data_directory: Optional[Text],
    story_directory: Optional[Text],
    output_path: Text,
    train_path: Text,
    force_training: bool = False,
    retrain_core: bool = True,
    retrain_nlu: bool = True,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
):

    if force_training or retrain_core:
        await _train_core_with_validated_data(
            domain=domain,
            config=config,
            story_directory=story_directory,
            output=output_path,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )
    else:
        print_color(
            "Core stories/configuration did not change. No need to retrain Core model.",
            color=bcolors.OKBLUE,
        )

    if force_training or retrain_nlu:
        _train_nlu_with_validated_data(
            config=config,
            nlu_data_directory=nlu_data_directory,
            output=output_path,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
        )
    else:
        print_color(
            "NLU data/configuration did not change. No need to retrain NLU model.",
            color=bcolors.OKBLUE,
        )


def train_core(
    domain: Union[Domain, Text],
    config: Text,
    stories: Text,
    output: Text,
    train_path: Optional[Text] = None,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
) -> Optional[Text]:
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(
        train_core_async(
            domain=domain,
            config=config,
            stories=stories,
            output=output,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )
    )


async def train_core_async(
    domain: Union[Domain, Text],
    config: Text,
    stories: Text,
    output: Text,
    train_path: Optional[Text] = None,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
) -> Optional[Text]:
    """Trains a Core model.

    Args:
        domain: Path to the domain file.
        config: Path to the config file for Core.
        stories: Path to the Core training data.
        output: Output path.
        train_path: If `None` the model will be trained in a temporary
            directory, otherwise in the provided directory.
        fixed_model_name: Name of model to be stored.
        uncompress: If `True` the model will not be compressed.
        kwargs: Additional training parameters.

    Returns:
        If `train_path` is given it returns the path to the model archive,
        otherwise the path to the directory with the trained model files.

    """

    skill_imports = SkillSelector.load(config, stories)

    try:
        domain = Domain.load(domain, skill_imports)
        domain.check_missing_templates()
    except InvalidDomain:
        print_error(
            "Core training was skipped because no valid domain file was found. "
            "Please specify a valid domain using '--domain' argument or check if the provided domain file exists."
        )
        return None

    train_context = TempDirectoryPath(data.get_core_directory(stories, skill_imports))

    with train_context as story_directory:
        if not os.listdir(story_directory):
            print_error(
                "No stories given. Please provide stories in order to "
                "train a Rasa Core model using the '--stories' argument."
            )
            return

        return await _train_core_with_validated_data(
            domain=domain,
            config=config,
            story_directory=story_directory,
            output=output,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
            kwargs=kwargs,
        )


async def _train_core_with_validated_data(
    domain: Domain,
    config: Text,
    story_directory: Text,
    output: Text,
    train_path: Optional[Text] = None,
    fixed_model_name: Optional[Text] = None,
    kwargs: Optional[Dict] = None,
) -> Optional[Text]:
    """Train Core with validated training and config data."""

    import rasa.core.train

    with ExitStack() as stack:
        if train_path:
            # If the train path was provided, do nothing on exit.
            _train_path = train_path
        else:
            # Otherwise, create a temp train path and clean it up on exit.
            _train_path = stack.enter_context(TempDirectoryPath(tempfile.mkdtemp()))

        # normal (not compare) training
        print_color("Training Core model...", color=bcolors.OKBLUE)
        await rasa.core.train(
            domain_file=domain,
            stories_file=story_directory,
            output_path=os.path.join(_train_path, "core"),
            policy_config=config,
            kwargs=kwargs,
        )
        print_color("Core model training completed.", color=bcolors.OKBLUE)

        if train_path is None:
            # Only Core was trained.
            new_fingerprint = model.model_fingerprint(
                config, domain, stories=story_directory
            )
            return _package_model(
                new_fingerprint=new_fingerprint,
                output_path=output,
                train_path=_train_path,
                fixed_model_name=fixed_model_name,
                model_prefix="core-",
            )

        return _train_path


def train_nlu(
    config: Text,
    nlu_data: Text,
    output: Text,
    train_path: Optional[Text] = None,
    fixed_model_name: Optional[Text] = None,
) -> Optional[Text]:
    """Trains an NLU model.

    Args:
        config: Path to the config file for NLU.
        nlu_data: Path to the NLU training data.
        output: Output path.
        train_path: If `None` the model will be trained in a temporary
            directory, otherwise in the provided directory.
        fixed_model_name: Name of the model to be stored.
        uncompress: If `True` the model will not be compressed.

    Returns:
        If `train_path` is given it returns the path to the model archive,
        otherwise the path to the directory with the trained model files.

    """

    # training NLU only hence the training files still have to be selected
    skill_imports = SkillSelector.load(config, nlu_data)
    train_context = TempDirectoryPath(data.get_nlu_directory(nlu_data, skill_imports))

    with train_context as nlu_data_directory:
        if not os.listdir(nlu_data_directory):
            print_error(
                "No NLU data given. Please provide NLU data in order to train "
                "a Rasa NLU model using the '--nlu' argument."
            )
            return

        return _train_nlu_with_validated_data(
            config=config,
            nlu_data_directory=nlu_data_directory,
            output=output,
            train_path=train_path,
            fixed_model_name=fixed_model_name,
        )


def _train_nlu_with_validated_data(
    config: Text,
    nlu_data_directory: Text,
    output: Text,
    train_path: Optional[Text] = None,
    fixed_model_name: Optional[Text] = None,
) -> Optional[Text]:
    """Train NLU with validated training and config data."""

    import rasa.nlu.train

    with ExitStack() as stack:
        if train_path:
            # If the train path was provided, do nothing on exit.
            _train_path = train_path
        else:
            # Otherwise, create a temp train path and clean it up on exit.
            _train_path = stack.enter_context(TempDirectoryPath(tempfile.mkdtemp()))

        print_color("Training NLU model...", color=bcolors.OKBLUE)
        _, nlu_model, _ = rasa.nlu.train(
            config, nlu_data_directory, _train_path, fixed_model_name="nlu"
        )
        print_color("NLU model training completed.", color=bcolors.OKBLUE)

        if train_path is None:
            # Only NLU was trained
            new_fingerprint = model.model_fingerprint(
                config, nlu_data=nlu_data_directory
            )

            return _package_model(
                new_fingerprint=new_fingerprint,
                output_path=output,
                train_path=_train_path,
                fixed_model_name=fixed_model_name,
                model_prefix="nlu-",
            )

        return _train_path


def _package_model(
    new_fingerprint: Fingerprint,
    output_path: Text,
    train_path: Text,
    fixed_model_name: Optional[Text] = None,
    model_prefix: Text = "",
):
    output_path = create_output_path(
        output_path, prefix=model_prefix, fixed_name=fixed_model_name
    )
    model.create_package_rasa(train_path, output_path, new_fingerprint)

    print_success(
        "Your Rasa model is trained and saved at '{}'.".format(
            os.path.abspath(output_path)
        )
    )

    return output_path
