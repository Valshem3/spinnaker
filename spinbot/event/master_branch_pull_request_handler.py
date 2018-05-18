from .handler import Handler
from .pull_request_event import GetBaseBranch, GetPullRequest, GetTitle
from gh import ReleaseBranchFor, ParseCommitMessage

format_message = ('The following commits need their title changed:\n\n{}\n\n' +
    'Please format your commit title into the form: \n\n' +
    '`<type>(<scope>): <subject>`, e.g. `fix(kubernetes): address NPE in status check`\n\n' +
    'This allows us to easily generate changelogs & determine semantic version numbers when ' +
    'cutting releases. You can read more about [commit ' +
    'conventions](https://www.spinnaker.io/community/contributing/submitting/#commit-message-conventions) ' +
    'here.')

class MasterBranchPullRequestHandler(Handler):
    def __init__(self):
        super().__init__()

    def handles(self, event):
        return (event.type == 'PullRequestEvent'
            and event.payload.get('action') == 'opened'
            and GetBaseBranch(event) == 'master')

    def handle(self, g, event):
        pull_request = GetPullRequest(g, event)
        if pull_request is None:
            self.logging.warn('Unable to determine PR that created {}'.format(event))
            return

        commits = pull_request.get_commits()
        bad_commits = []
        
        for commit in commits:
            message = ParseCommitMessage(commit.commit.message)
            if message is None:
                bad_commits.append(commit.commit)
        
        if len(bad_commits) > 0:
            pull_request.create_issue_comment(format_message.format(
                ''.join(map(lambda c: '{}: {}'.format(c.sha, c.message), bad_commits))
            ))

MasterBranchPullRequestHandler()
