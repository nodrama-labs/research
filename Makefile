# Research repo: the notebook produces feed.json (run the "Export dashboard
# feed" cell in notebooks/regime_student_t_drawdown_2022.org). This Makefile's
# only job is to publish that artifact to the blog repo, which owns the
# dashboard presentation and commits feed.json.
#
#   make publish        # copy feed.json -> <blog>/regimes/feed.json
#   make publish BLOG=/path/to/blog
#
# feed.json is gitignored here; it is committed in the blog repo.

BLOG ?= $(HOME)/CloudStation/DevOps/fbielejec.github.io
FEED := feed.json
DEST := $(BLOG)/regimes/feed.json

.PHONY: publish

publish:
	@test -f $(FEED) || { echo "$(FEED) not found — run the notebook's 'Export dashboard feed' cell first"; exit 1; }
	@test -d "$(BLOG)/regimes" || { echo "blog regimes/ dir not found at $(BLOG)/regimes"; exit 1; }
	cp $(FEED) "$(DEST)"
	@echo "published $(FEED) -> $(DEST)"
	@echo "now commit it in the blog repo: (cd $(BLOG) && git add regimes/feed.json && git commit)"
