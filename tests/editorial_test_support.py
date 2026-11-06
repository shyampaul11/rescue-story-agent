"""Keep AppTest reruns away from real private editorial workspaces."""
from functools import wraps
from unittest.mock import patch


def isolated_editorial_storage(test_class):
    """Patch app storage before each existing setUp; unittest owns cleanup."""
    original_setup = test_class.setUp

    @wraps(original_setup)
    def set_up(self):
        for target, options in (('content_ui.load_record', {'return_value': None}),
                                ('content_ui.save_record', {})):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        original_setup(self)

    test_class.setUp = set_up
    return test_class
