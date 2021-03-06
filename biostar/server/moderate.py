"""
Moderator views
"""
from biostar.apps.posts.models import Post, Vote
from biostar.apps.badges.models import Award
from biostar.apps.posts.auth import post_permissions
from biostar.apps.users.models import User
from biostar.apps.users.auth import user_permissions
from biostar.apps.util import html
from django.conf import settings
from django.views.generic import FormView
from django.shortcuts import render
from django.contrib import messages
from biostar import const
from braces.views import LoginRequiredMixin
from django import forms
from django.core.urlresolvers import reverse
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Fieldset, Submit, ButtonHolder
from django.http import HttpResponseRedirect
from django.db.models import Q, F
from datetime import timedelta
import logging
from datetime import datetime
from django.utils.timezone import utc

logger = logging.getLogger(__name__)

OPEN, CLOSE_OFFTOPIC, CLOSE_SPAM, DELETE, \
    DUPLICATE, MOVE_TO_COMMENT, MOVE_TO_ANSWER, CROSSPOST, TOGGLE_ACCEPT, BUMP_POST = map(str, range(10))

from biostar.apps.util import now

POST_LIMIT_ERROR_MSG = '''
<p><b>Sorry!</b> Your posting limit of (%s) posts per six hours has been reached.</p>
<p>This limit is very low for new users and is raised as you gain reputation.</p>
<p>This limit is necessary to protect the site from automated postings by spammers.</p>
'''

TOP_POST_LIMIT_ERROR_MSG = '''
<p><b>Sorry!</b> Your posting limit of (%s) questions per six hours has been reached.
Note that you can still contribute with comments and answers though.</p>
<p>This limit is very low for new users and is raised as you gain reputation.</p>
<p>This limit is necessary to protect the site from automated postings by spammers.</p>
'''

def update_user_status(user):
    "A user needs to have votes supporting them"
    if user.score >= settings.TRUST_VOTE_COUNT and not user.is_trusted:
        user.status = User.TRUSTED
        user.save()
    return user

def user_exceeds_limits(request, top_level=False):
    """
    Puts on limits on how many posts a user can post.
    """
    user = request.user
    since = now() - timedelta(hours=6)

    # Check the user's credentials.
    user = update_user_status(user)

    # How many posts were generated by this user today.
    all_post_count = Post.objects.filter(author=user, creation_date__gt=since).count()

    # How many top level posts were generated by this user today.
    top_post_count = Post.objects.filter(author=user, creation_date__gt=since, type__in=Post.TOP_LEVEL).count()

    # The number of posts a user can create.
    max_post_limit = settings.MAX_POSTS_TRUSTED_USER if user.is_trusted else settings.MAX_POSTS_NEW_USER

    # The number of top level posts a user may create
    max_top_post_limit = settings.MAX_TOP_POSTS_TRUSTED_USER if user.is_trusted else settings.MAX_TOP_POSTS_NEW_USER

    # # Apply the limit checks.
    # if (all_post_count + 1) > max_post_limit:
    #     messages.info(request, POST_LIMIT_ERROR_MSG % max_post_limit)
    #     logger.error("post limit reached for %s" % user)
    #     return False
    #
    # # This only needs to be checked when creating top level post
    # if top_level and ((top_post_count + 1) > max_top_post_limit):
    #     messages.info(request, TOP_POST_LIMIT_ERROR_MSG % max_top_post_limit)
    #     logger.error("top post limit reached for %s" % user)
    #     return False

    return False

class PostModForm(forms.Form):
    CHOICES = [
        (BUMP_POST, "Bump a post"),
        (OPEN, "Open a closed or deleted post"),
        (TOGGLE_ACCEPT, "Toggle accepted status"),
        (MOVE_TO_ANSWER, "Move post to an answer"),
        (MOVE_TO_COMMENT, "Move post to a comment on the top level post"),
        (DUPLICATE, "Duplicated post (top level)"),
        (CROSSPOST, "Cross posted at other site"),
        (CLOSE_OFFTOPIC, "Close post (top level)"),
        (DELETE, "Delete post"),
    ]

    action = forms.ChoiceField(choices=CHOICES, widget=forms.RadioSelect(), label="Select Action")

    comment = forms.CharField(required=False, max_length=200,
                              help_text="Enter a reason (required when closing, crosspost). This will be inserted into a template comment.")

    dupe = forms.CharField(required=False, max_length=200,
                           help_text="One or more duplicated post numbers, space or comma separated (required for duplicate closing).",
                           label="Duplicate number(s)")

    def __init__(self, *args, **kwargs):
        pk = kwargs['pk']
        kwargs.pop('pk')
        super(PostModForm, self).__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.error_text_inline = False
        self.helper.help_text_inline = True
        self.helper.form_action = reverse("post-moderation", kwargs=dict(pk=pk))

        self.helper.layout = Layout(
            Fieldset(
                'Select moderation option',
                'action',
                'comment',
                'dupe',
            ),
            ButtonHolder(
                Submit('submit', 'Submit')
            )
        )

    def clean(self):
        cleaned_data = super(PostModForm, self).clean()
        action = cleaned_data.get("action")
        comment = cleaned_data.get("comment")
        dupe = cleaned_data.get("dupe")

        if action == CLOSE_OFFTOPIC and not comment:
            raise forms.ValidationError("Unable to close. Please add a comment!")

        if action == CROSSPOST and not comment:
            raise forms.ValidationError("Please add URL into the comment!")

        if action == DUPLICATE and not dupe:
            raise forms.ValidationError("Unable to close duplicate. Please fill in the post numbers")

        if dupe:
            dupe = dupe.replace(",", " ")
            dupes = dupe.split()[:5]
            cleaned_data['dupe'] = dupes

        return cleaned_data

class PostModeration(LoginRequiredMixin, FormView):
    model = Post
    template_name = "post_moderation_form.html"
    context_object_name = "post"
    form_class = PostModForm

    def get_obj(self):
        pk = self.kwargs['pk']
        obj = Post.objects.get(pk=pk)
        return obj

    def get(self, request, *args, **kwargs):
        post = self.get_obj()
        post = post_permissions(request, post)
        if not post.is_editable:
            messages.warning(request, "You may not moderate this post")
            return HttpResponseRedirect(post.root.get_absolute_url())
        form = self.form_class(pk=post.id)
        context = dict(form=form, post=post)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        user = request.user

        post = self.get_obj()
        post = post_permissions(request, post)

        # The default return url
        response = HttpResponseRedirect(post.root.get_absolute_url())

        if not post.is_editable:
            messages.warning(request, "You may not moderate this post")
            return response

        # Initialize the form class.
        form = self.form_class(request.POST, pk=post.id)

        # Bail out on errors.
        if not form.is_valid():
            messages.error(request, "%s" % form.errors)
            return response

        # A shortcut to the clean form data.
        get = form.cleaned_data.get

        # These will be used in updates, will bypasses signals.
        query = Post.objects.filter(pk=post.id)
        root  = Post.objects.filter(pk=post.root_id)

        action = get('action')

        if action == (BUMP_POST) and post != post.root:
            messages.error(request, "Only top-level posts may be bumped!")
            return response

        if action == (BUMP_POST) and user.is_moderator:
            now = datetime.utcnow().replace(tzinfo=utc)
            Post.objects.filter(id=post.id).update(lastedit_date=now, lastedit_user=request.user)
            messages.success(request, "Post bumped")
            return response

        if action == (OPEN, TOGGLE_ACCEPT) and not user.is_moderator:
            messages.error(request, "Only a moderator may open or toggle a post")
            return response

        if action == TOGGLE_ACCEPT and post.type == Post.ANSWER:
            # Toggle post acceptance.
            post.has_accepted=not post.has_accepted
            post.save()
            has_accepted = Post.objects.filter(root=post.root, type=Post.ANSWER, has_accepted=True).count()
            root.update(has_accepted=has_accepted)
            return response

        if action == MOVE_TO_ANSWER and post.type == Post.COMMENT:
            # This is a valid action only for comments.
            messages.success(request, "Moved post to answer")
            query.update(type=Post.ANSWER, parent=post.root)
            root.update(reply_count=F("reply_count") + 1)
            return response

        if action == MOVE_TO_COMMENT and post.type == Post.ANSWER:
            # This is a valid action only for answers.
            messages.success(request, "Moved post to answer")
            query.update(type=Post.COMMENT, parent=post.root)
            root.update(reply_count=F("reply_count") - 1)
            return response

        # Some actions are valid on top level posts only.
        if action in (CLOSE_OFFTOPIC, DUPLICATE) and not post.is_toplevel:
            messages.warning(request, "You can only close or open a top level post")
            return response

        if action == OPEN:
            query.update(status=Post.OPEN)
            messages.success(request, "Opened post: %s" % post.title)
            return response

        if action in CLOSE_OFFTOPIC:
            query.update(status=Post.CLOSED)
            messages.success(request, "Closed post: %s" % post.title)
            content = html.render(name="messages/offtopic_posts.html", user=post.author, comment=get("comment"), post=post)
            comment = Post(content=content, type=Post.COMMENT, parent=post, author=user)
            comment.save()
            return response

        if action == CROSSPOST:
            content = html.render(name="messages/crossposted.html", user=post.author, comment=get("comment"), post=post)
            comment = Post(content=content, type=Post.COMMENT, parent=post, author=user)
            comment.save()
            return response

        if action == DUPLICATE:
            query.update(status=Post.CLOSED)
            posts = Post.objects.filter(id__in=get("dupe"))
            content = html.render(name="messages/duplicate_posts.html", user=post.author, comment=get("comment"), posts=posts)
            comment = Post(content=content, type=Post.COMMENT, parent=post, author=user)
            comment.save()
            return response

        if action == DELETE:

            # Delete marks a post deleted but does not remove it.
            # Remove means to delete the post from the database with no trace.

            # Posts with children or older than some value can only be deleted not removed

            # The children of a post.
            children = Post.objects.filter(parent_id=post.id).exclude(pk=post.id)

            # The condition where post can only be deleted.
            delete_only = children or post.age_in_days > 7 or post.vote_count > 1 or (post.author != user)

            if delete_only:
                # Deleted posts can be undeleted by re-opening them.
                query.update(status=Post.DELETED)
                messages.success(request, "Deleted post: %s" % post.title)
                response = HttpResponseRedirect(post.root.get_absolute_url())
            else:
                # This will remove the post. Redirect depends on the level of the post.
                url = "/" if post.is_toplevel else post.parent.get_absolute_url()
                post.delete()
                messages.success(request, "Removed post: %s" % post.title)
                response = HttpResponseRedirect(url)

            # Recompute post reply count
            post.update_reply_count()

            return response

        # By this time all actions should have been performed
        messages.warning(request, "That seems to be an invalid action for that post. \
                It is probably ok! Actions may be shown even when not valid.")
        return response

class UserModForm(forms.Form):
    CHOICES = [
        (User.NEW_USER, "Reinstate as new user"),
        (User.TRUSTED, "Reinstate as trusted user"),
        (User.SUSPENDED, "Suspend user"),
        (User.BANNED, "Ban user"),
    ]

    action = forms.ChoiceField(choices=CHOICES, widget=forms.RadioSelect(), label="Select Action")

    def __init__(self, *args, **kwargs):
        pk = kwargs['pk']
        kwargs.pop('pk')
        super(UserModForm, self).__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.error_text_inline = False
        self.helper.help_text_inline = True
        self.helper.form_action = reverse("user-moderation", kwargs=dict(pk=pk))

        self.helper.layout = Layout(
            Fieldset(
                'Select action',
                'action',
            ),
            ButtonHolder(
                Submit('submit', 'Submit')
            )
        )


class UserModeration(LoginRequiredMixin, FormView):
    model = Post
    template_name = "user_moderation_form.html"
    context_object_name = "user"
    form_class = UserModForm

    def get_obj(self):
        pk = self.kwargs['pk']
        obj = User.objects.get(pk=pk)
        return obj

    def get(self, request, *args, **kwargs):
        user = request.user
        target = self.get_obj()
        target = user_permissions(request, target)

        form = self.form_class(pk=target.id)
        context = dict(form=form, target=target)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        user = request.user

        target = self.get_obj()
        target = user_permissions(request, target)
        profile = target.profile

        # The response after the action
        response = HttpResponseRedirect(target.get_absolute_url())

        if target.is_administrator:
            messages.warning(request, "Cannot moderate an administrator")
            return response

        if user == target:
            messages.warning(request, "Cannot moderate yourself")
            return response

        if not user.is_moderator:
            messages.warning(request, "Only moderators have this permission")
            return response

        if not target.is_editable:
            messages.warning(request, "Target not editable by this user")
            return response

        form = self.form_class(request.POST, pk=target.id)
        if not form.is_valid():
            messages.error(request, "Invalid user modification action")
            return response

        action = int(form.cleaned_data['action'])

        if action == User.BANNED and not user.is_administrator:
            messages.error(request, "Only administrators may ban users")
            return response

        if action == User.BANNED and user.is_administrator:
            # Remove data by user
            profile.clear_data()

            # Lets make sure we don't ban people that have been around a while
            # These can still be removed but via the admin interface
            # We do this to limit damage that a hacked admin account could do.
            if target.score > 3:
                messages.error(request, "Target user has a high score and can only be banned via the admin interface")
                return response

            # Remove badges that may have been earned by this user.
            Award.objects.filter(user=target).delete()

            # Delete all votes by this user.
            Vote.objects.filter(author=target).delete()

            # Mark all posts as deleted.
            Post.objects.filter(author=target).update(status=Post.DELETED)

            # Destroy posts with no votes.
            query = Post.objects.filter(author=target, vote_count__lt=2)
            count = query.count()
            query.delete()

            messages.success(request, "User banned, %s posts removed" % count)


        # Apply the new status
        User.objects.filter(pk=target.id).update(status=action)

        messages.success(request, 'Moderation completed')
        return response

