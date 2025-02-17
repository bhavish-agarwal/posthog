from posthog.models import FunnelStep, Action, ActionStep, Event, Funnel, Person, PersonDistinctId
from rest_framework import request, response, serializers, viewsets # type: ignore
from rest_framework.decorators import action # type: ignore
from django.db.models import QuerySet, query, Model, Q, Max, Prefetch, Exists, OuterRef, Subquery
from django.db import models
from typing import List, Dict, Any


class FunnelSerializer(serializers.HyperlinkedModelSerializer):
    steps = serializers.SerializerMethodField()

    class Meta:
        model = Funnel
        fields = ['id', 'name', 'deleted', 'steps']

    def _order_people_in_step(self, steps: List[Dict[str, Any]], people: List[int]) -> List[int]:
        def order(person):
            score = 0
            for step in steps:
                if person in step['people']:
                    score += 1
            return (score, person)
        return sorted(people, key=order, reverse=True)

    def get_steps(self, funnel: Funnel) -> List[Dict[str, Any]]:
        # for some reason, rest_framework executes SerializerMethodField multiple times,
        # causing lots of slow queries. 
        # Seems a known issue: https://stackoverflow.com/questions/55023511/serializer-being-called-multiple-times-django-python
        if hasattr(funnel, 'steps_cache'):
            return []
        funnel.steps_cache = True # type: ignore

        funnel_steps = funnel.steps.all().order_by('order').prefetch_related('action')
        if self.context['view'].action != 'retrieve' or self.context['request'].GET.get('exclude_count'):
            return [{
                'id': step.id,
                'action_id': step.action.id,
                'name': step.action.name,
                'order': step.order
            } for step in funnel_steps]

        if len(funnel_steps) == 0:
            return []
        annotations = {}
        for index, step in enumerate(funnel_steps):
            annotations['step_{}'.format(index)] = Subquery(
                Event.objects.filter_by_action(step.action) # type: ignore
                    .annotate(person_id=OuterRef('id'))
                    .filter(
                        team_id=funnel.team_id,
                        distinct_id__in=Subquery(
                            PersonDistinctId.objects.filter(
                                team_id=funnel.team_id,
                                person_id=OuterRef('person_id')
                            ).values('distinct_id')
                        ),
                        **({'timestamp__gt': OuterRef('step_{}'.format(index-1))} if index > 0 else {})
                    )\
                    .order_by('timestamp')\
                    .values('timestamp')[:1])

        people = Person.objects.all()\
            .filter(team_id=funnel.team_id, persondistinctid__distinct_id__isnull=False)\
            .annotate(**annotations)\
            .filter(step_0__isnull=False)\
            .distinct('pk')

        steps = []
        for index, step in enumerate(funnel_steps):
            relevant_people = [person.id for person in people if getattr(person, 'step_{}'.format(index))]
            steps.append({
                'id': step.id,
                'action_id': step.action.id,
                'name': step.action.name,
                'order': step.order,
                'people': relevant_people[:100],
                'count': len(relevant_people)
            })
        if len(steps) > 0:
            steps[0]['people'] = self._order_people_in_step(steps, steps[0]['people'])
        return steps

    def create(self, validated_data: Dict, *args: Any, **kwargs: Any) -> Funnel:
        request = self.context['request']
        funnel = Funnel.objects.create(team=request.user.team_set.get(), created_by=request.user, **validated_data)
        if request.data.get('steps'):
            for index, step in enumerate(request.data['steps']):
                if step.get('action_id'):
                    FunnelStep.objects.create(
                        funnel=funnel,
                        action_id=step['action_id'],
                        order=index
                    )
        return funnel

    def update(self, funnel: Funnel, validated_data: Any) -> Funnel: # type: ignore
        request = self.context['request']

        funnel.deleted = validated_data.get('deleted', funnel.deleted)
        funnel.name = validated_data.get('name', funnel.name)
        funnel.save()

        # If there's no steps property at all we just ignore it
        # If there is a step property but it's an empty array [], we'll delete all the steps
        if 'steps' in request.data:
            steps = request.data.pop('steps')

            steps_to_delete = funnel.steps.exclude(pk__in=[step.get('id') for step in steps if step.get('id') and '-' not in str(step['id'])])
            steps_to_delete.delete()
            for index, step in enumerate(steps):
                if step.get('action_id'):
                    # make sure it's not a uuid, in which case we can just ignore id
                    if step.get('id') and '-' not in str(step['id']):
                        db_step = FunnelStep.objects.get(funnel=funnel, pk=step['id'])
                        db_step.action_id = step['action_id']
                        db_step.order = index
                        db_step.save()
                    else:
                        FunnelStep.objects.create(
                            funnel=funnel,
                            order=index,
                            action_id=step['action_id']
                        )
        return funnel

class FunnelViewSet(viewsets.ModelViewSet):
    queryset = Funnel.objects.all()
    serializer_class = FunnelSerializer

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        if self.action == 'list': # type: ignore
            queryset = queryset.filter(deleted=False)
        return queryset\
            .filter(team=self.request.user.team_set.get())
